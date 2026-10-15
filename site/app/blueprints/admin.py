from flask import Blueprint, render_template, flash, redirect, url_for, request, jsonify, abort, current_app, session
from flask_login import login_required, current_user
# Importa todos os decorators de auth.py
from app.blueprints.auth import (
    master_required, admin_required, master_or_aprovador_required,
    viewer_required, supremo_required, normalizar_telefone_usuario,
    cidades_do_usuario,
)
from app import db
from app import jobstore
from app.navegacao import voltar
import secrets
from app.models import (
    Usuario, Config, FlagAnticheat, ControleDisciplinar, Atleta, Activity,
    Feedback, TIPO_SUPREMO, TIPO_VIEWER, ROTULOS_TIPO,
)
from app.forms import RegistrationForm # Usaremos o RegistrationForm para adicionar e editar usuários
from datetime import datetime, timedelta # Para campos de auditoria
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path

# Configurar logger
logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

# Quantos atletas podem receber o MESMO nome numa passada antes de a
# atualizacao ser abortada. Tres: duas pessoas com nome igual acontece
# (o proprio banco tem 'Fulana Exemplo' tres vezes, em perfis diferentes);
# tres nomes identicos vindos da MESMA leitura, nao.
_MAX_MESMO_NOME = 3
_MAINTENANCE_JOBS = {}
_MAINTENANCE_LOCK = threading.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKUP_DIR = _PROJECT_ROOT.parent / 'Backups'
# AUDITADO EM 26/08/2026 (teste seco, arquivo por arquivo).
# ENTRARAM: `CLAUDE.md` -- 50 KB de contexto do projeto que nao existe em
# nenhum outro lugar e ficava de fora; `test_dashboard.py`; e a pasta
# `scripts/`, que e' codigo e tambem ficava de fora.
# SAIRAM: `movimente_se.ini` e `requirements-temp.txt`, que nao existem mais no
# projeto (nao quebravam nada, mas mentiam sobre o que o backup cobre).
_BACKUP_ROOT_FILES = {
    '.env',
    '.gitignore',
    'CLAUDE.md',
    'config.py',
    'gunicorn_config.py',
    'init_db.py',
    'requirements.txt',
    'test_dashboard.py',
    'uwsgi.ini',
    'wsgi.py',
}
# 20/09/2026: `static` e `templates` da RAIZ sairam do projeto (o que vale
# e' `app/static` e `app/templates`); `test_app.py` tambem. Lista que cita
# o que nao existe mente sobre o que o backup cobre.
_BACKUP_ROOT_DIRS = {'app', 'instance', 'logs', 'migrations', 'scripts',
                     'uploads'}
_BACKUP_EXCLUDED_DIRS = {
    '.git', 'venv', 'temp_extract', 'nginx_tmp',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
}
# zip/txt entram no backup: sao os dados-fonte dos uploads, nao recriaveis.
_BACKUP_EXCLUDED_UPLOAD_SUBDIRS = set()
_BACKUP_EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.tmp'}
_BACKUP_EXCLUDED_FILES = {'wsgi_debug.txt', 'PROJECT_CONSOLIDATED.txt'}
# secrets.json entra no backup a pedido do usuario: perder a chave de
# assinatura e o cookie do Strava numa restauracao custa mais do que o risco de
# te-los dentro do zip -- que ja carrega o banco inteiro, com hashes de senha.
# Na restauracao ele so volta se NAO existir um no servidor (ver _run_restore_job).
_BACKUP_ALLOWED_INSTANCE_FILES = {'movimente_se.db', 'secrets.json'}
_SECRETS_RELATIVE_PATH = Path('instance') / 'secrets.json'
_SQLITE_DB_RELATIVE_PATH = Path('instance') / 'movimente_se.db'


def _new_maintenance_job(kind):
    job_id = str(uuid.uuid4())
    with _MAINTENANCE_LOCK:
        _MAINTENANCE_JOBS[job_id] = {
            'kind': kind,
            'stage': 'queued',
            'message': 'Aguardando inicio...',
            'pct': 1,
            'done': False,
            'ok': None,
            'artifact': '',
            'created_at': time.time(),
        }
        # Espelho em arquivo: o polling pode cair no outro worker do uWSGI.
        jobstore.write(f'maint_{job_id}', dict(_MAINTENANCE_JOBS[job_id]))
    return job_id


def _set_maintenance_job(job_id, **kwargs):
    with _MAINTENANCE_LOCK:
        job = _MAINTENANCE_JOBS.get(job_id)
        if not job:
            job = jobstore.read(f'maint_{job_id}')
            if not job:
                return
            _MAINTENANCE_JOBS[job_id] = job
        job.update(kwargs)
        job['updated_at'] = time.time()
        jobstore.write(f'maint_{job_id}', dict(job))


def _expire_maintenance_job(job_id, delay=900):
    def _do():
        time.sleep(delay)
        with _MAINTENANCE_LOCK:
            _MAINTENANCE_JOBS.pop(job_id, None)
        jobstore.delete(f'maint_{job_id}')
    threading.Thread(target=_do, daemon=True).start()


def _validate_admin_csrf():
    from flask_wtf.csrf import validate_csrf
    from wtforms import ValidationError
    token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken', '')
    try:
        validate_csrf(token)
        return None
    except ValidationError as exc:
        return jsonify(status='error', message=f'Token CSRF invalido: {exc}'), 400


def _is_inside_project(path):
    try:
        path.resolve().relative_to(_PROJECT_ROOT.resolve())
        return True
    except ValueError:
        return False


def _skip_backup_dir(rel_parts):
    if not rel_parts:
        return False
    if any(part in _BACKUP_EXCLUDED_DIRS for part in rel_parts):
        return True
    if len(rel_parts) >= 2 and rel_parts[0] == 'uploads' and rel_parts[1] in _BACKUP_EXCLUDED_UPLOAD_SUBDIRS:
        return True
    return False


def _skip_backup_file(path):
    rel = path.relative_to(_PROJECT_ROOT)
    if path.name in _BACKUP_EXCLUDED_FILES:
        return True
    if path.suffix.lower() in _BACKUP_EXCLUDED_SUFFIXES:
        return True
    if rel.parts and rel.parts[0] == 'instance' and path.name not in _BACKUP_ALLOWED_INSTANCE_FILES:
        return True
    return False


def _iter_backup_files():
    files = []

    for name in sorted(_BACKUP_ROOT_FILES):
        path = _PROJECT_ROOT / name
        if path.is_file() and not _skip_backup_file(path):
            files.append(path)

    for dirname in sorted(_BACKUP_ROOT_DIRS):
        base = _PROJECT_ROOT / dirname
        if not base.is_dir():
            continue
        for root, dirs, filenames in os.walk(base):
            root_path = Path(root)
            rel_parts = root_path.relative_to(_PROJECT_ROOT).parts
            dirs[:] = [
                d for d in dirs
                if not _skip_backup_dir(rel_parts + (d,))
            ]
            if _skip_backup_dir(rel_parts):
                continue
            for filename in filenames:
                path = root_path / filename
                if _skip_backup_file(path):
                    continue
                files.append(path)

    return sorted(files, key=lambda p: str(p.relative_to(_PROJECT_ROOT)).lower())


def _create_sqlite_snapshot(source_path):
    tmp_file = tempfile.NamedTemporaryFile(prefix='movimente_se_db_', suffix='.db', delete=False)
    snapshot_path = Path(tmp_file.name)
    tmp_file.close()
    source_conn = None
    backup_conn = None

    try:
        source_conn = sqlite3.connect(str(source_path), timeout=30)
        try:
            # Com journal WAL, garante que transacoes recentes estejam no arquivo
            # principal antes do snapshot (a API .backup ja cobre, cinto duplo).
            source_conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            pass
        backup_conn = sqlite3.connect(str(snapshot_path))
        source_conn.backup(backup_conn)
        return snapshot_path
    except Exception:
        try:
            snapshot_path.unlink()
        except OSError:
            pass
        raise
    finally:
        if backup_conn:
            backup_conn.close()
        if source_conn:
            source_conn.close()


def _verificar_snapshot(caminho):
    """Um backup que ninguem abriu nao e um backup.

    Abre a copia, roda integrity_check e confere que as tabelas que importam
    responderam. So depois disso a exclusao e liberada.
    """
    con = None
    try:
        con = sqlite3.connect(f'file:{caminho}?mode=ro', uri=True, timeout=30)
        estado = con.execute('PRAGMA integrity_check').fetchone()[0]
        if str(estado).lower() != 'ok':
            return False, f'integrity_check={estado}'
        for tabela in ('usuarios', 'config', 'activities'):
            con.execute(f'SELECT COUNT(*) FROM {tabela}').fetchone()
        return True, 'ok'
    except Exception as exc:
        return False, str(exc)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def backup_antes_de_apagar(motivo):
    """Backup completo do banco, TESTADO, antes de qualquer exclusao.

    Decisao do operador (24/07): apagar cidade ou usuario so acontece com
    backup garantido. Se o backup falhar ou nao abrir, nada e apagado.
    Devolve (ok, caminho_ou_erro).
    """
    origem = _PROJECT_ROOT / _SQLITE_DB_RELATIVE_PATH
    if not origem.is_file():
        return False, 'banco de dados nao encontrado'
    snapshot = None
    try:
        snapshot = _create_sqlite_snapshot(origem)
        ok, detalhe = _verificar_snapshot(snapshot)
        if not ok:
            return False, f'backup gerado mas ilegivel: {detalhe}'
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        seguro = re.sub(r'[^A-Za-z0-9_-]+', '-', str(motivo or 'exclusao'))[:60]
        destino = _BACKUP_DIR / f'pre-exclusao_{seguro}_{stamp}.db'
        shutil.move(str(snapshot), str(destino))
        # Os -wal/-shm que a verificacao deixou nao servem para nada e so
        # confundem quem olha a pasta.
        from app.backupauto import _limpar_sidecars
        _limpar_sidecars(snapshot)
        _limpar_sidecars(destino)
        snapshot = None
        logger.info('Backup pre-exclusao verificado e salvo: %s', destino)
        return True, str(destino)
    except Exception as exc:
        logger.error('Falha ao gerar backup pre-exclusao (%s): %s', motivo, exc, exc_info=True)
        return False, str(exc)
    finally:
        if snapshot is not None:
            try:
                Path(snapshot).unlink()
            except OSError:
                pass


def _run_backup_job(job_id):
    sqlite_snapshot = None
    try:
        _set_maintenance_job(job_id, stage='scan', message='Mapeando arquivos essenciais...', pct=3)
        files = _iter_backup_files()
        if not files:
            raise RuntimeError('Nenhum arquivo elegivel encontrado para backup.')

        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = _BACKUP_DIR / f'movimente-se_backup_{stamp}.zip'
        sqlite_source = _PROJECT_ROOT / _SQLITE_DB_RELATIVE_PATH
        file_sizes = {}

        if sqlite_source in files and sqlite_source.is_file():
            _set_maintenance_job(
                job_id,
                stage='snapshot',
                message='Criando snapshot consistente do banco de dados...',
                pct=4,
            )
            sqlite_snapshot = _create_sqlite_snapshot(sqlite_source)

        for path in files:
            size_path = sqlite_snapshot if path == sqlite_source and sqlite_snapshot else path
            file_sizes[path] = size_path.stat().st_size if size_path.is_file() else 0
        total_bytes = sum(file_sizes.values())

        copied = 0
        _set_maintenance_job(
            job_id,
            stage='copy',
            message=f'Criando backup em {backup_path}...',
            pct=5,
        )
        with zipfile.ZipFile(backup_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
            for index, path in enumerate(files, start=1):
                rel = path.relative_to(_PROJECT_ROOT).as_posix()
                source_for_zip = sqlite_snapshot if path == sqlite_source and sqlite_snapshot else path
                zf.write(source_for_zip, arcname=rel)
                copied += file_sizes.get(path, 0)
                if index == len(files) or index % 10 == 0:
                    pct = 5 + int((copied / max(total_bytes, 1)) * 92)
                    _set_maintenance_job(
                        job_id,
                        stage='copy',
                        message=f'Copiando {index}/{len(files)} arquivos: {rel}',
                        pct=min(pct, 97),
                    )

        # ESTE BACKUP NUNCA TEVE ROTACAO (26/08/2026): eram 11 copias somando
        # 1,17 GB, sem nada para apagar as antigas. Agora ele usa a mesma
        # retencao avo-pai-filho dos backups automaticos.
        removidos = 0
        try:
            from app.backupauto import (GFS_DIARIOS, GFS_MENSAIS, GFS_SEMANAIS,
                                        _rotacionar_gfs)
            removidos = _rotacionar_gfs('movimente-se_backup', GFS_DIARIOS,
                                        GFS_SEMANAIS, GFS_MENSAIS)
        except Exception as exc:
            logger.warning('Rotacao dos backups manuais falhou: %s', exc)

        recado = f'Backup concluido em {backup_path}'
        if removidos:
            recado += f' ({removidos} backup(s) antigo(s) removido(s) na rotacao)'
        _set_maintenance_job(
            job_id,
            stage='done',
            message=recado,
            pct=100,
            done=True,
            ok=True,
            artifact=str(backup_path),
        )
        logger.info('Backup master concluido: %s (%s arquivos, %s antigo(s) removido(s))',
                    backup_path, len(files), removidos)
    except Exception as exc:
        logger.error('Erro no backup master: %s', exc, exc_info=True)
        _set_maintenance_job(
            job_id,
            stage='error',
            message=f'Erro ao gerar backup: {exc}',
            pct=0,
            done=True,
            ok=False,
        )
    finally:
        if sqlite_snapshot:
            try:
                sqlite_snapshot.unlink()
            except OSError:
                pass
        _expire_maintenance_job(job_id)


_BACKUP_NAME_RE = re.compile(r'^movimente-se_backup_\d{8}_\d{6}\.zip$')
# Restauracao repoe apenas DADOS; codigo (.py/templates) nunca e sobrescrito.
_RESTORE_DATA_PREFIXES = ('uploads/', 'static/images/')


def _list_backup_zips():
    try:
        if not _BACKUP_DIR.is_dir():
            return []
        zips = [
            p.name for p in _BACKUP_DIR.iterdir()
            if p.is_file() and _BACKUP_NAME_RE.match(p.name)
        ]
        return sorted(zips, reverse=True)
    except OSError:
        return []


def _safe_backup_member(name):
    """Aceita apenas caminhos relativos simples dentro do zip (sem .. ou raiz)."""
    if not name or name.startswith('/') or '\\' in name:
        return False
    return '..' not in name.split('/')


def _run_restore_job(job_id, backup_name):
    db_snapshot = None
    try:
        backup_path = _BACKUP_DIR / backup_name
        _set_maintenance_job(job_id, stage='scan',
                             message=f'Validando backup {backup_name}...', pct=3)
        if not backup_path.is_file():
            raise RuntimeError('Arquivo de backup nao encontrado.')

        with zipfile.ZipFile(backup_path, 'r') as zf:
            names = zf.namelist()
            db_member = _SQLITE_DB_RELATIVE_PATH.as_posix()
            if db_member not in names:
                raise RuntimeError('Backup nao contem instance/movimente_se.db.')
            bad = [n for n in names if not _safe_backup_member(n)]
            if bad:
                raise RuntimeError(f'Backup contem caminhos invalidos: {bad[:3]}')

            # 1) Banco: extrair para arquivo temporario e validar integridade
            _set_maintenance_job(job_id, stage='snapshot',
                                 message='Extraindo e validando o banco do backup...', pct=15)
            tmp_file = tempfile.NamedTemporaryFile(prefix='restore_db_', suffix='.db', delete=False)
            db_snapshot = Path(tmp_file.name)
            tmp_file.close()
            with zf.open(db_member) as src, open(db_snapshot, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            check_conn = sqlite3.connect(str(db_snapshot))
            try:
                result = check_conn.execute('PRAGMA integrity_check').fetchone()
            finally:
                check_conn.close()
            if not result or str(result[0]).lower() != 'ok':
                raise RuntimeError('Banco do backup reprovado no integrity_check.')

            # 2) Aplicar o banco por cima do atual via API de backup do SQLite
            #    (forma segura de substituir um banco com a aplicacao no ar)
            _set_maintenance_job(job_id, stage='copy',
                                 message='Restaurando banco de dados...', pct=35)
            live_db = _PROJECT_ROOT / _SQLITE_DB_RELATIVE_PATH
            src_conn = sqlite3.connect(str(db_snapshot))
            dst_conn = sqlite3.connect(str(live_db), timeout=60)
            try:
                src_conn.backup(dst_conn)
            finally:
                src_conn.close()
                dst_conn.close()

            # 2b) Cofre de segredos: so restaura se o servidor nao tiver um.
            #     Assim uma restauracao de rotina nao troca a chave em uso (o que
            #     deslogaria todo mundo), mas um NAS reconstruido do zero recupera
            #     a chave de assinatura e o cookie do Strava.
            secrets_member = _SECRETS_RELATIVE_PATH.as_posix()
            secrets_destino = _PROJECT_ROOT / _SECRETS_RELATIVE_PATH
            if secrets_member in names:
                if secrets_destino.exists():
                    logger.info('Restauracao: cofre de segredos preservado (ja existe no servidor).')
                else:
                    _set_maintenance_job(job_id, stage='copy',
                                         message='Restaurando cofre de segredos...', pct=38)
                    secrets_destino.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(secrets_member) as src, open(secrets_destino, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    try:
                        os.chmod(secrets_destino, 0o600)
                    except OSError:
                        pass
                    logger.warning('Restauracao: cofre de segredos recuperado do backup.')

            # 3) Dados de uploads e imagens (nunca codigo .py/templates)
            data_members = [
                n for n in names
                if n.startswith(_RESTORE_DATA_PREFIXES) and not n.endswith('/')
            ]
            total = len(data_members)
            for index, member in enumerate(data_members, start=1):
                dest = _PROJECT_ROOT / Path(member)
                if not _is_inside_project(dest):
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                if index == total or index % 20 == 0:
                    pct = 40 + int((index / max(total, 1)) * 55)
                    _set_maintenance_job(
                        job_id, stage='copy',
                        message=f'Restaurando dados {index}/{total}: {member}',
                        pct=min(pct, 97),
                    )

        _set_maintenance_job(
            job_id, stage='done',
            message=('Restauracao concluida (banco, uploads e imagens). '
                     'Reinicie o perfil web no DSM para recarregar a aplicacao.'),
            pct=100, done=True, ok=True, artifact=str(backup_path),
        )
        logger.warning('Restauracao master concluida a partir de %s', backup_path)
    except Exception as exc:
        logger.error('Erro na restauracao: %s', exc, exc_info=True)
        _set_maintenance_job(
            job_id, stage='error',
            message=f'Erro na restauracao: {exc}',
            pct=0, done=True, ok=False,
        )
    finally:
        if db_snapshot:
            try:
                db_snapshot.unlink()
            except OSError:
                pass
        _expire_maintenance_job(job_id)


def _cache_targets():
    targets = []
    ignored_roots = {'.git', 'venv'}
    cache_dirs = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}

    for root, dirs, files in os.walk(_PROJECT_ROOT):
        root_path = Path(root)
        rel_parts = root_path.relative_to(_PROJECT_ROOT).parts
        dirs[:] = [d for d in dirs if d not in ignored_roots]

        for dirname in list(dirs):
            if dirname in cache_dirs:
                target = root_path / dirname
                if _is_inside_project(target):
                    targets.append(target)
                dirs.remove(dirname)

        for filename in files:
            path = root_path / filename
            if path.suffix.lower() in ('.pyc', '.pyo') and _is_inside_project(path):
                targets.append(path)

    return targets


def _run_faxina_job(app, job_id, pedido_por=''):
    """Faxina sob demanda: banco enxuto e pasta de trabalhos limpa.

    A MESMA faxina que roda sozinha 1x por dia (`_rotina_diaria`). Existe aqui
    como botao porque quem esta olhando a tela de manutencao quer o resultado
    agora, e porque um numero na tela ("liberou 12 MB") vale mais que uma linha
    no log que ninguem le.

    O QUE ELA NUNCA APAGA (pedido do dono, 26/08/2026)
    Nada que seja dado do sistema: nenhum km, nenhuma atividade, nenhum atleta,
    nenhum ranking, nenhum patrocinador. Ela mexe em tres coisas, todas
    descartaveis por definicao: a IMAGEM de mensagem que ja foi entregue (a
    linha do envio fica), item de fila entregue ha mais de 60 dias, e arquivo
    de acompanhamento de trabalho ja concluido. Trabalho vivo nao e' tocado.
    """
    from sqlalchemy import text as sa_text

    from app import filamensagens as _fila
    from app.blueprints.upload import faxina_de_jobs

    # A thread nasce fora do request: sem isto, `db.session` nao existe aqui.
    with app.app_context():
        try:
            _set_maintenance_job(job_id, stage='scan',
                                 message='Medindo o que da para liberar...', pct=5)
            antes = 0
            try:
                antes = (db.session.execute(sa_text(
                    'SELECT COALESCE(SUM(LENGTH(imagem_blob)), 0) FROM fila_mensagens'
                )).scalar() or 0)
            except Exception:
                db.session.rollback()

            _set_maintenance_job(job_id, stage='clean',
                                 message='Liberando imagens de mensagens ja entregues...', pct=25)
            imagens = _fila.aliviar_imagens_entregues()

            _set_maintenance_job(job_id, stage='clean',
                                 message='Removendo itens de fila antigos...', pct=50)
            antigas = _fila.limpar_antigas()

            _set_maintenance_job(job_id, stage='clean',
                                 message='Limpando a pasta de trabalhos...', pct=75)
            jobs = faxina_de_jobs()

            depois = 0
            try:
                depois = (db.session.execute(sa_text(
                    'SELECT COALESCE(SUM(LENGTH(imagem_blob)), 0) FROM fila_mensagens'
                )).scalar() or 0)
            except Exception:
                db.session.rollback()

            liberado_mb = max(0, antes - depois) / 1048576.0
            partes = []
            if imagens:
                partes.append('%d imagem(ns) de mensagem ja entregue' % imagens)
            if antigas:
                partes.append('%d item(ns) de fila antigo(s)' % antigas)
            if jobs.get('apagados'):
                partes.append('%d acompanhamento(s) de trabalho concluido' % jobs['apagados'])
            if jobs.get('cache'):
                partes.append('%d cache(s) vencido(s)' % jobs['cache'])
            if jobs.get('encerrados'):
                partes.append('%d trabalho(s) abandonado(s) encerrado(s)' % jobs['encerrados'])

            if partes:
                recado = 'Faxina concluida: ' + ', '.join(partes) + '.'
                if liberado_mb >= 0.1:
                    # O arquivo do banco nao encolhe na hora: o espaco volta para
                    # reuso do SQLite. Encolher exigiria VACUUM, que tranca o banco
                    # inteiro -- num NAS lento, remedio pior que a doenca (regra 24).
                    recado += (' Liberou %.1f MB dentro do banco (o arquivo nao '
                               'diminui de tamanho, mas o espaco volta a ser usado).'
                               % liberado_mb)
            else:
                recado = 'Nada a limpar: ja estava tudo enxuto.'

            _set_maintenance_job(job_id, stage='done', message=recado,
                                 pct=100, done=True, ok=True)
            logger.info('Faxina manual por %s: %s', pedido_por or 'desconhecido', recado)
        except Exception as exc:
            db.session.rollback()
            logger.error('Faxina manual falhou: %s', exc, exc_info=True)
            _set_maintenance_job(job_id, stage='error',
                                 message='A faxina falhou: %s. Nada foi perdido.' % exc,
                                 pct=0, done=True, ok=False)
        finally:
            _expire_maintenance_job(job_id)


def _run_clear_cache_job(job_id):
    try:
        _set_maintenance_job(job_id, stage='scan', message='Localizando caches Python seguros...', pct=5)
        targets = _cache_targets()
        total = len(targets)
        removed = 0

        if total == 0:
            _set_maintenance_job(
                job_id,
                stage='done',
                message='Nenhum cache Python encontrado para limpar.',
                pct=100,
                done=True,
                ok=True,
            )
            return

        for index, target in enumerate(targets, start=1):
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.is_file():
                    target.unlink()
                removed += 1
            except Exception as exc:
                logger.warning('Falha ao remover cache %s: %s', target, exc)

            if index == total or index % 5 == 0:
                _set_maintenance_job(
                    job_id,
                    stage='clean',
                    message=f'Limpando cache {index}/{total}: {target.name}',
                    pct=5 + int((index / max(total, 1)) * 92),
                )

        _set_maintenance_job(
            job_id,
            stage='done',
            message=f'Limpeza concluida: {removed}/{total} itens de cache removidos.',
            pct=100,
            done=True,
            ok=True,
        )
        logger.info('Limpeza de cache master concluida: %s/%s itens', removed, total)
    except Exception as exc:
        logger.error('Erro na limpeza de cache master: %s', exc, exc_info=True)
        _set_maintenance_job(
            job_id,
            stage='error',
            message=f'Erro ao limpar cache: {exc}',
            pct=0,
            done=True,
            ok=False,
        )
    finally:
        _expire_maintenance_job(job_id)

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
@viewer_required  # 20/09/2026 (item 73): TODO perfil entra no painel — o catálogo filtra tela a tela
def dashboard():
    """Painel de administração — o ponto de partida, por assunto."""
    logger.info(f"Master admin {current_user.email} acessou o dashboard.")

    from app import secretstore
    strava_info = secretstore.info(secretstore.STRAVA_SESSION, scope=current_user.cidade_ativa)
    strava_saude = secretstore.session_health(current_user.cidade_ativa)

    # Os cards do painel saem do catalogo (app/indice.py), nunca escritos aqui
    # na mao: era a lista escrita a mao que deixava tela orfa. Falhar aqui nao
    # derruba o painel — sem cards ele ainda mostra os avisos e o indice.
    try:
        from app import indice as _indice
        categorias = _indice.painel(current_app)
        # CADA PERFIL CONTA SO O QUE VE (19/09/2026, itens 72/73): o card dizia
        # "4 telas" de Manutencao para quem via 1. Mesma regra da tela do
        # assunto (`area.html`): sem nivel, ou o usuario tem aquele nivel.
        categorias = [dict(c, telas=[
            t for t in c['telas']
            if getattr(current_user, t.get('nivel') or _indice.NIVEL_PADRAO, False)])
                      for c in categorias]
        categorias = [c for c in categorias if c['telas']]
        indice_total, indice_orfas = _indice.resumo(current_app)
    except Exception as exc:
        logger.warning('Nao consegui montar os cards do painel: %s', exc)
        categorias, indice_total, indice_orfas = [], 0, 0

    # Estado do MODO TESTE (P-27) — o mais importante do painel: com ele
    # ligado, nada chega ao grupo.
    _esc = current_user.cidade_ativa
    modo_teste = {
        'ligado': (secretstore.get(secretstore.MODO_TESTE, scope=_esc)
                   or '').strip().lower() == 'sim',
        'destino': (secretstore.get(secretstore.MODO_TESTE_DESTINO, scope=_esc)
                    or '').strip(),
        # Grupo de teste: em ensaio, o que é de grupo vai para cá em vez de
        # virar mensagem privada (que não testa menção nem formato).
        'grupo': (secretstore.get(secretstore.MODO_TESTE_GRUPO, scope=_esc)
                  or '').strip(),
    }

    # MANTER LIGADA (17/08/2026, item 4.1-b; escopo corrigido no D3 do plano
    # attach-tidy-globe) — só existe para o master supremo, e o escopo é a(s)
    # cidade(s) com token de automação cadastrado, não `cidade_ativa` (que ele
    # troca pela tela e não tem relação com qual máquina física existe).
    manter_ligada = {'ligado': False, 'visivel': bool(current_user.is_supremo)}

    # A AUTOMACAO ESTA DE PE ATE QUANDO? (09/09/2026)
    # Bloco PROPRIO no painel, separado de "manter a maquina ligada": um fala
    # do computador, o outro dos containers da aplicacao. Juntar os dois numa
    # caixa so' faria o operador clicar num achando que e' o outro.
    automacao_de_pe = {'ativo': False, 'texto': '', 'por': '',
                       'visivel': bool(current_user.is_supremo)}
    try:
        from sqlalchemy import text as _sa_text
        from app.blueprints.api import _ensure_estado_schema, _janela_containers
        _ensure_estado_schema()
        automacao_de_pe.update(_janela_containers(current_user.cidade_ativa) or {})
        automacao_de_pe['ativo'] = bool(automacao_de_pe.get('containers_ativo'))
        automacao_de_pe['texto'] = automacao_de_pe.get('containers_texto') or ''
        automacao_de_pe['por'] = automacao_de_pe.get('containers_por') or ''
        # O TITULO DIZ O QUE SUBIU, NAO O QUE FOI PEDIDO (18/09/2026, Pendente
        # 15). O prazo e gravado no clique; quem sobe e o Guardiao, minutos
        # depois (medido: 10:51:15 -> 10:58:03). So pergunta com pedido ativo:
        # perguntar ao Dell dormindo o acorda (item 22), e aqui acordar e o
        # que se quer.
        automacao_de_pe['subiu'] = None
        if automacao_de_pe['ativo']:
            from app import maquina as _maq, whatsapp as _wa
            _casa = _wa.container_da_cidade(current_user.cidade_ativa)
            if _casa and _casa.get('host') and _casa.get('porta'):
                automacao_de_pe['subiu'] = _maq.porta_tem_waha(
                    _casa['host'], int(_casa['porta'])) is True
    except Exception as _exc:
        logger.debug('painel: nao consegui ler a janela dos containers (%s)', _exc)
    if current_user.is_supremo:
        cidades_com_automacao = [
            (escopo or '').strip()
            for escopo, _ in secretstore.entries(secretstore.AUTOMATION_TOKEN)
            if (escopo or '').strip()
        ]
        manter_ligada['ligado'] = any(
            (secretstore.get(secretstore.MANTER_LIGADA, scope=c) or '').strip().lower() == 'sim'
            for c in cidades_com_automacao
        )

    # O pedido de "publicar agora", para o painel mostrar em que pé está.
    try:
        from app import rodada as _rodada
        pedido_ranking = _rodada.estado(_esc)
    except Exception as exc:
        logger.warning('Nao consegui ver o pedido de rodada: %s', exc)
        pedido_ranking = {'tem': False}

    return render_template('admin/dashboard.html',
                           categorias=categorias,
                           strava_info=strava_info,
                           strava_saude=strava_saude,
                           modo_teste=modo_teste,
                           manter_ligada=manter_ligada,
                           automacao_de_pe=automacao_de_pe,
                           pedido_ranking=pedido_ranking,
                           indice_total=indice_total,
                           indice_orfas=indice_orfas)


@admin_bp.route('/area/<chave>')
@login_required
@viewer_required   # 20/09/2026 (item 73): o portao de verdade e' o nivel de cada tela
def area(chave):
    """As telas de um assunto só, em cards — o segundo nível do painel."""
    from app import indice as _indice
    cat = _indice.categoria(current_app, chave)
    if cat is None:
        flash('Esse assunto não existe no painel.', 'warning')
        return redirect(url_for('admin.dashboard'))

    # SEM NENHUMA TELA QUE ESTA PESSOA POSSA ABRIR, O ASSUNTO NAO EXISTE PARA
    # ELA (19/09/2026, item 73): o painel ja nao mostra o card; digitar o
    # endereco abria a moldura vazia. Mesma regra do card e do `area.html`.
    if not any(getattr(current_user, t.get('nivel') or _indice.NIVEL_PADRAO, False)
               for t in cat['telas']):
        flash('Esse assunto não faz parte do seu acesso.', 'warning')
        return redirect(url_for('admin.dashboard'))

    # Numero em cima do card, quando ele diz alguma coisa util de relance.
    # So estes dois: contador que ninguem usa e enfeite que custa consulta.
    metricas = {}
    if chave == 'cadastros':
        metricas = {'admin.manage_users': Usuario.query.count(),
                    'admin.manage_cities': Config.query.count()}

    return render_template('admin/area.html', cat=cat, metricas=metricas)


@admin_bp.route('/manutencao')
@login_required
@supremo_required    # 19/09/2026: manutencao so o master supremo (regra do dono)
def manutencao():
    """Backup, restauração e limpeza de cache."""
    return render_template('admin/manutencao.html', backups=_list_backup_zips())


@admin_bp.route('/maintenance/backup/start', methods=['POST'])
@login_required
@supremo_required    # 19/09/2026: manutencao so o master supremo (regra do dono)
def start_backup():
    csrf_err = _validate_admin_csrf()
    if csrf_err:
        return csrf_err
    job_id = _new_maintenance_job('backup')
    threading.Thread(target=_run_backup_job, args=(job_id,), daemon=True).start()
    return jsonify(status='ok', job_id=job_id)


@admin_bp.route('/maintenance/cache/start', methods=['POST'])
@login_required
@supremo_required    # 19/09/2026: manutencao so o master supremo (regra do dono)
def start_clear_cache():
    csrf_err = _validate_admin_csrf()
    if csrf_err:
        return csrf_err
    job_id = _new_maintenance_job('cache')
    threading.Thread(target=_run_clear_cache_job, args=(job_id,), daemon=True).start()
    return jsonify(status='ok', job_id=job_id)


@admin_bp.route('/maintenance/faxina/start', methods=['POST'])
@login_required
@supremo_required    # 19/09/2026: manutencao so o master supremo (regra do dono)
def start_faxina():
    csrf_err = _validate_admin_csrf()
    if csrf_err:
        return csrf_err
    job_id = _new_maintenance_job('faxina')
    # `current_user` nao existe dentro da thread: o email vai junto, capturado
    # aqui, ainda dentro do request.
    threading.Thread(target=_run_faxina_job,
                     args=(current_app._get_current_object(), job_id, current_user.email),
                     daemon=True).start()
    return jsonify(status='ok', job_id=job_id)


@admin_bp.route('/maintenance/restore/start', methods=['POST'])
@login_required
@supremo_required    # 19/09/2026: manutencao so o master supremo (regra do dono)
def start_restore():
    csrf_err = _validate_admin_csrf()
    if csrf_err:
        return csrf_err
    backup_name = (request.form.get('backup_name') or '').strip()
    confirmacao = (request.form.get('confirmacao') or '').strip().upper()
    if not _BACKUP_NAME_RE.match(backup_name) or backup_name not in _list_backup_zips():
        return jsonify(status='error', message='Backup invalido ou nao encontrado.'), 400
    if confirmacao != 'RESTAURAR':
        return jsonify(status='error',
                       message='Confirmacao invalida. Digite RESTAURAR para prosseguir.'), 400
    job_id = _new_maintenance_job('restore')
    threading.Thread(target=_run_restore_job, args=(job_id, backup_name), daemon=True).start()
    logger.warning('Restauracao iniciada por %s a partir de %s', current_user.email, backup_name)
    return jsonify(status='ok', job_id=job_id)


@admin_bp.route('/maintenance/status/<job_id>')
@login_required
@master_required
def maintenance_status(job_id):
    with _MAINTENANCE_LOCK:
        job = dict(_MAINTENANCE_JOBS.get(job_id) or {})
    if not job:
        # O job pode estar rodando no outro worker do uWSGI: consultar o espelho.
        job = jobstore.read(f'maint_{job_id}') or {}
    if not job:
        # ATUALIZACAO DE PERFIS AGORA MORA NA FILA (07/09/2026), entao o
        # andamento dela esta no arquivo de trabalho, nao no espelho acima.
        try:
            from app.blueprints.upload import _read_progress_file, _TERMINAL_STAGES
            fila = _read_progress_file(job_id) or {}
            if fila:
                estagio = fila.get('stage') or ''
                job = {
                    'kind': 'perfis',
                    'stage': estagio,
                    'message': fila.get('message') or '',
                    'pct': int(fila.get('pct') or 0),
                    'done': estagio in _TERMINAL_STAGES,
                    'ok': estagio == 'done',
                }
        except Exception as exc:
            logger.debug('Nao consegui ler o trabalho %s na fila: %s', job_id, exc)
    if not job:
        return jsonify(status='unknown', message='Job nao encontrado ou ja expirado.', pct=0, done=True, ok=False)
    return jsonify(status='ok', **job)

def _cidades_gerenciaveis():
    """Cidades onde este usuario pode criar/mover gente.

    O supremo alcanca todas; os demais ficam na propria. Fronteira de cidade
    e regra de seguranca, nao conveniencia de tela.
    """
    if current_user.is_supremo:
        return [c.cidade for c in Config.query.order_by(Config.cidade).all()]
    return [current_user.cidade_ativa]


def _aplicar_escolhas_de_tipo(form):
    """Deixa no seletor apenas os niveis que quem esta logado pode conceder."""
    permitidos = set(current_user.tipos_que_pode_conceder())
    form.tipo.choices = [c for c in form.tipo.choices if c[0] in permitidos]
    if not form.tipo.choices:
        form.tipo.choices = [(TIPO_VIEWER, ROTULOS_TIPO[TIPO_VIEWER])]
    validos = {c[0] for c in form.tipo.choices}
    if form.tipo.data not in validos:
        form.tipo.data = form.tipo.choices[0][0]


@admin_bp.route('/manage_users')
@login_required
@master_or_aprovador_required # <<< ATUALIZADO: Master OU Aprovador podem gerenciar usuários
def manage_users():
    """Página para gerenciar usuários."""
    if current_user.is_supremo:
        users = Usuario.query.order_by(Usuario.cidade, Usuario.nome).all()
    else:
        users = Usuario.query.filter_by(cidade=current_user.cidade_ativa).order_by(Usuario.nome).all()
    return render_template('admin/manage_users.html', users=users,
                           cidades=_cidades_gerenciaveis())

@admin_bp.route('/add_user', methods=['GET', 'POST'])
@login_required
@master_or_aprovador_required # <<< ATUALIZADO: Master OU Aprovador podem adicionar usuários
def add_user():
    """Adicionar um novo usuário pelo master admin ou aprovador."""
    form = RegistrationForm()

    cidades = _cidades_gerenciaveis()
    form.cidade.choices = [(c, c) for c in cidades]
    if not form.cidade.choices: # Garante que há pelo menos uma opção se não houver cidades cadastradas
        form.cidade.choices = [('', 'Nenhuma cidade cadastrada. Crie uma primeiro.')]
    _aplicar_escolhas_de_tipo(form)

    if form.validate_on_submit():
        nome = form.nome.data.strip()
        email = form.email.data.strip().lower()
        password = form.password.data
        cidade = form.cidade.data
        tipo = form.tipo.data # <<< NOVIDADE: Pega o tipo do formulário
        ativo = form.ativo.data # <<< NOVIDADE: Pega o status ativo do formulário
        telefone = normalizar_telefone_usuario(form.telefone.data)

        # Ninguem cria alguem mais forte que si mesmo, nem fora da propria cidade.
        if tipo not in current_user.tipos_que_pode_conceder():
            flash('Você não pode criar um usuário com esse nível de acesso.', 'error')
            return render_template('admin/add_user.html', form=form)
        if cidade not in cidades:
            flash('Você não pode criar usuários nessa cidade.', 'error')
            return render_template('admin/add_user.html', form=form)

        # 22/08/2026 — EXIGENCIA DE @exemplo.com REMOVIDA (decisao do dono).
        # O tenant Microsoft 365 E5 tem so 16 licencas; amarrar o cadastro ao
        # dominio da empresa limitava o projeto a 16 pessoas. Aqui o risco era
        # ainda menor que no cadastro publico: quem cria o usuario JA E um
        # admin autenticado, que responde pelo que cria. Ver P-103 (confirmacao
        # de e-mail) para a camada de seguranca que entra no lugar.

        if not telefone:
            flash('Telefone inválido. Use DDD + número, como (DD) 9XXXX-XXXX.', 'error')
            return render_template('admin/add_user.html', form=form)

        existing_user = Usuario.query.filter_by(email=email).first()
        if existing_user:
            flash('Email já cadastrado no sistema.', 'error')
            return render_template('admin/add_user.html', form=form) # Retorna para o form com a flash message

        if Usuario.query.filter_by(telefone=telefone).first():
            flash('Telefone já cadastrado para outro usuário.', 'error')
            return render_template('admin/add_user.html', form=form)

        new_user = Usuario(
            nome=nome,
            email=email,
            cidade=cidade,
            telefone=telefone,
            tipo=tipo, # <<< ATUALIZADO: Usa o tipo do formulário
            ativo=ativo, # <<< ATUALIZADO: Usa o status ativo do formulário
            # Senha digitada pelo admin e provisoria por definicao: serve para
            # entrar uma vez e trocar. Senha previsivel que dura e senha vazada.
            senha_provisoria=True,
            criado_em=datetime.utcnow(),
            gerado_por_id=current_user.id, # <<< NOVIDADE: Registra quem criou o usuário
            ult_modificacao=datetime.utcnow(),
            modificado_por_id=current_user.id # NOVIDADE
        )
        new_user.set_password(password)

        try:
            db.session.add(new_user)
            db.session.commit()
            flash(f"Usuário '{email}' adicionado.", "success")
            logger.info(f"Admin {current_user.email} adicionou o usuário {email} (tipo: {tipo}, ativo: {ativo}).")

            # CONVITE POR E-MAIL (20/09/2026, P-103): a pessoa cria a PROPRIA
            # senha por um link que chega no e-mail dela. A senha digitada aqui
            # continua existindo como plano B (e ja nasce provisoria), mas o
            # caminho normal deixa de ser "te mando a senha por WhatsApp".
            try:
                from app import contas
                convidado, detalhe, link_convite = contas.convidar(
                    new_user, por=current_user.email)
            except Exception as exc:
                convidado, detalhe, link_convite = False, str(exc), ''
                logger.error('add_user: falha ao convidar %s: %s', email, exc)
            if convidado:
                flash('Convite enviado para %s — ele mesmo cria a senha dele.'
                      % email, 'success')
            else:
                flash('Não consegui enviar o convite para %s (%s). Entregue o '
                      'link abaixo, ou avise que a senha provisória é a que '
                      'você digitou.' % (email, detalhe), 'warning')
                if link_convite:
                    flash(link_convite, 'reset_link')

            # ===== O WHATSAPP DELE JA SAI PEDIDO DAQUI =====
            # Quem publica precisa do WhatsApp ligado ao sistema para servir de
            # alguma coisa. Deixar isso para "depois, naquela outra tela" e
            # garantir que metade dos cadastros fique pela metade — e a cidade
            # descobre no dia em que o ranking nao sai.
            #
            # A tela de Sessao do WhatsApp continua existindo para os casos
            # avulsos: reconectar quem caiu, ou cadastrar um numero fora do
            # fluxo normal.
            if new_user.ativo and new_user.telefone and (new_user.ordem_envio or 0):
                try:
                    from app import whatsapp as wa, ligarmaquina
                    if ligarmaquina.configurado(cidade):
                        ligarmaquina.ligar(cidade)
                    pediu, aviso = wa.pedir_vinculacao(
                        cidade, new_user.telefone, pedido_por=current_user.email,
                        usuario=new_user, email=new_user.email)
                    if pediu:
                        flash('WhatsApp de %s: %s' % (new_user.nome, aviso), 'info')
                except Exception as exc:
                    # Cadastro feito e cadastro feito: falhar aqui nao pode
                    # desfazer o usuario nem assustar quem acabou de cria-lo.
                    logger.warning('Nao consegui pedir o WhatsApp de %s: %s', email, exc)
                    flash('Usuário criado, mas não consegui iniciar a conexão do '
                          'WhatsApp dele. Faça pela tela Sessão do WhatsApp.', 'warning')
            elif new_user.ativo and new_user.telefone:
                flash('Para o WhatsApp de %s entrar na automação, defina a ordem '
                      'de envio dele em "Quem publica no grupo".' % new_user.nome,
                      'info')

            return redirect(url_for('admin.manage_users')) # <<< CORRIGIDO: Redirecionamento limpo
        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao adicionar usuário '{email}': {e}", exc_info=True)
            flash(f"Erro ao adicionar usuário: {e}", "error")

    return render_template('admin/add_user.html', form=form)

@admin_bp.route('/edit_user/<int:user_id>', methods=['GET', 'POST'])
@login_required
@master_or_aprovador_required # <<< NOVIDADE: Master OU Aprovador podem editar usuários
def edit_user(user_id):
    """Editar um usuário existente."""
    user_to_edit = Usuario.query.get_or_404(user_id)

    if not current_user.pode_gerir(user_to_edit):
        flash('Acesso negado. Você não tem permissão para editar este usuário.', 'error')
        return redirect(url_for('admin.manage_users'))

    # Criar uma instância do RegistrationForm e preencher com os dados do usuário
    form = RegistrationForm(obj=user_to_edit) # Preenche o formulário com dados do usuário

    # Cidades para onde este usuario pode ser TRANSFERIDO (decisao de 24/07:
    # cada pessoa pertence a uma cidade, com opcao de mudar de cidade).
    cidades = _cidades_gerenciaveis()
    if user_to_edit.cidade not in cidades:
        cidades = [user_to_edit.cidade] + cidades
    form.cidade.choices = [(c, c) for c in cidades]
    _aplicar_escolhas_de_tipo(form)
    # O nivel atual do editado precisa continuar aparecendo, senao salvar a tela
    # rebaixaria a pessoa sem ninguem pedir.
    if user_to_edit.tipo not in {c[0] for c in form.tipo.choices}:
        form.tipo.choices = [(user_to_edit.tipo, ROTULOS_TIPO.get(user_to_edit.tipo, user_to_edit.tipo))] + form.tipo.choices

    # Remover validadores de senha para edição, já que não vamos mudar a senha aqui
    # Os campos de senha serão ocultos no template HTML para este contexto
    form.password.validators = []
    form.confirm_password.validators = []

    if request.method == 'POST' and form.validate_on_submit():
        novo_tipo = form.tipo.data
        nova_cidade = form.cidade.data
        telefone = normalizar_telefone_usuario(form.telefone.data)
        permitidos = set(current_user.tipos_que_pode_conceder()) | {user_to_edit.tipo}
        if novo_tipo not in permitidos:
            flash('Você não pode conceder esse nível de acesso.', 'error')
            return render_template('admin/edit_user.html', form=form, user=user_to_edit)
        if nova_cidade not in cidades:
            flash('Você não pode mover o usuário para essa cidade.', 'error')
            return render_template('admin/edit_user.html', form=form, user=user_to_edit)
        if not telefone:
            flash('Telefone inválido. Use DDD + número, como (DD) 9XXXX-XXXX.', 'error')
            return render_template('admin/edit_user.html', form=form, user=user_to_edit)
        ja_usa = Usuario.query.filter(Usuario.telefone == telefone,
                                      Usuario.id != user_to_edit.id).first()
        if ja_usa:
            flash('Telefone já cadastrado para outro usuário.', 'error')
            return render_template('admin/edit_user.html', form=form, user=user_to_edit)

        cidade_anterior = user_to_edit.cidade
        # Atualiza os dados do usuário com os dados do formulário
        user_to_edit.nome = form.nome.data.strip()
        user_to_edit.email = form.email.data.strip().lower() # Email ainda pode ser editado pelo master, mas readonly no form
        user_to_edit.cidade = nova_cidade
        user_to_edit.telefone = telefone
        user_to_edit.tipo = novo_tipo
        user_to_edit.ativo = form.ativo.data
        user_to_edit.modificado_por_id = current_user.id # <<< NOVIDADE: Registra quem modificou
        user_to_edit.ult_modificacao = datetime.utcnow() # <<< NOVIDADE: Data da modificação

        try:
            db.session.commit()
            if cidade_anterior != nova_cidade:
                logger.info('Usuario %s transferido de %s para %s por %s',
                            user_to_edit.email, cidade_anterior, nova_cidade, current_user.email)
                flash(f"Usuário transferido de {cidade_anterior} para {nova_cidade}.", "info")
            flash(f"Usuário '{user_to_edit.email}' atualizado com sucesso!", "success")
            logger.info(f"Admin {current_user.email} editou o usuário {user_to_edit.email}.")
            return redirect(url_for('admin.manage_users')) # <<< CORRIGIDO: Redirecionamento limpo
        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao editar usuário '{user_to_edit.email}': {e}", exc_info=True)
            flash(f"Erro ao editar usuário: {e}", "error")

    # GET = exibir o formulário preenchido
    return render_template('admin/edit_user.html', form=form, user=user_to_edit)


@admin_bp.route('/delete_user/<int:user_id>', methods=['POST'])
@login_required
@master_required # <<< NOVIDADE: Apenas Master pode deletar usuários
def delete_user(user_id):
    """Deletar um usuário — com backup do banco antes, sempre."""
    user_to_delete = Usuario.query.get_or_404(user_id)

    # Salvaguarda: Master Admin não pode deletar sua própria conta
    if user_to_delete.id == current_user.id:
        flash("Você não pode deletar sua própria conta!", "warning")
        return redirect(url_for('admin.manage_users'))

    if not current_user.pode_gerir(user_to_delete):
        flash('Sem permissão para excluir este usuário.', 'error')
        return redirect(url_for('admin.manage_users'))

    # A instalacao nunca pode ficar sem dono.
    if user_to_delete.is_supremo and Usuario.query.filter_by(tipo=TIPO_SUPREMO).count() <= 1:
        flash('Este é o único master supremo. Promova outro antes de excluir.', 'error')
        return redirect(url_for('admin.manage_users'))

    ok, detalhe = backup_antes_de_apagar(f'usuario-{user_to_delete.id}')
    if not ok:
        flash(f'Exclusão cancelada: não consegui gerar um backup confiável ({detalhe}).', 'error')
        return redirect(url_for('admin.manage_users'))

    try:
        email_apagado = user_to_delete.email
        db.session.delete(user_to_delete)
        db.session.commit()
        flash(f"Usuário '{email_apagado}' excluído. Backup salvo em {detalhe}.", "success")
        logger.info(f"Master admin {current_user.email} excluiu o usuário {email_apagado} (backup: {detalhe}).")
        return redirect(url_for('admin.manage_users')) # <<< CORRIGIDO: Redirecionamento limpo
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao deletar usuário '{user_to_delete.email}': {e}", exc_info=True)
        flash(f"Erro ao deletar usuário: {e}", "error")

    return redirect(url_for('admin.manage_users')) # Fallback de redirecionamento


@admin_bp.route('/toggle_user_status/<int:user_id>')
@login_required
@master_or_aprovador_required # <<< ATUALIZADO: Master OU Aprovador podem alternar status
def toggle_user_status(user_id):
    """Ativar/Desativar usuário."""
    user = Usuario.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Você não pode desativar sua própria conta!", "warning")
        return redirect(url_for('admin.manage_users'))

    if not current_user.pode_desativar(user):
        flash('Sem permissão para alterar o status deste usuário.', 'error')
        return redirect(url_for('admin.manage_users'))

    if (user.is_supremo and user.ativo
            and Usuario.query.filter_by(tipo=TIPO_SUPREMO, ativo=True).count() <= 1):
        flash('Este é o único master supremo ativo. Promova outro antes de desativá-lo.', 'error')
        return redirect(url_for('admin.manage_users'))

    user.ativo = not user.ativo
    user.modificado_por_id = current_user.id # <<< NOVIDADE: Registra quem modificou
    user.ult_modificacao = datetime.utcnow() # <<< NOVIDADE: Data da modificação

    try:
        db.session.commit()
        flash(f"Usuário '{user.email}' foi {'ativado' if user.ativo else 'desativado'} com sucesso.", "success")
        logger.info(f"Admin {current_user.email} alterou status do usuário {user.email} para {user.ativo}.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao alterar status do usuário '{user.email}': {e}", exc_info=True)
        flash(f"Erro ao alterar status do usuário: {e}", "error")
    return redirect(url_for('admin.manage_users'))


@admin_bp.route('/generate_reset/<int:user_id>', methods=['POST'])
@login_required
@master_or_aprovador_required
def generate_reset(user_id):
    """Gera link de redefinição de senha para um usuário (válido por 24h)."""
    from datetime import timedelta
    user = Usuario.query.get_or_404(user_id)

    if not current_user.pode_gerir(user):
        flash('Sem permissão para resetar senha deste usuário.', 'error')
        return redirect(url_for('admin.manage_users'))

    token = secrets.token_urlsafe(32)
    user.reset_token        = token
    user.reset_token_expiry = datetime.utcnow() + timedelta(hours=24)
    db.session.commit()

    reset_url = url_for('auth.reset_password', token=token, _external=True)
    logger.info(f"Token de reset gerado por {current_user.email} para {user.email}")

    # O LINK VAI PARA O DONO DA CONTA, NAO PARA A TELA DE QUEM CLICOU
    # (20/09/2026, P-103): resetar senha aqui passa a ser "avisar a pessoa",
    # nao "pegar o acesso dela". O link continua aparecendo na tela porque o
    # e-mail pode falhar — e aí quem reseta ainda consegue entregar o acesso
    # (regra 10: falhou, avisa; silêncio é o pior desfecho).
    try:
        from app import contas
        enviado, detalhe, reset_url = contas.pedir_nova_senha(
            user, por=current_user.email)
    except Exception as exc:
        enviado, detalhe = False, str(exc)
        logger.error('generate_reset: falha ao enviar e-mail: %s', exc)
    if enviado:
        flash('Link de nova senha enviado para %s.' % user.email, 'success')
    else:
        flash('Não consegui enviar o e-mail para %s (%s). Use o link abaixo.'
              % (user.email, detalhe), 'warning')
        # Categoria especial: base.html renderiza caixa de texto com botao Copiar.
        flash(reset_url, 'reset_link')
    return redirect(url_for('admin.manage_users'))


@admin_bp.route('/manage_cities')
@login_required
@supremo_required  # 19/09/2026 (item 73): abrir, ativar e apagar cidade e do supremo
def manage_cities():
    """Página para gerenciar cidades."""
    if current_user.is_supremo:
        cities = Config.query.order_by(Config.cidade).all()
    else:
        cities = Config.query.filter_by(cidade=current_user.cidade_ativa).all()
    # Quantos dados cada cidade carrega: a tela avisa antes, o backup garante
    # depois. O botao de excluir nunca some (decisao de 24/07) -- a protecao e
    # o backup obrigatorio, nao esconder o botao.
    resumo = {}
    for c in cities:
        resumo[c.cidade] = {
            'atividades': Activity.query.filter_by(cidade=c.cidade).count(),
            'usuarios': Usuario.query.filter_by(cidade=c.cidade).count(),
        }
    from app import consolidado as cons
    ativas = set(cons.cidades_ativas())
    return render_template('admin/manage_cities.html', cities=cities,
                           resumo=resumo, ativas=ativas)

def _ler_piso_importacao(form):
    """Le 'mes/ano de fundacao do grupo' do form. Os dois vazios = sem piso
    de cidade (fica so o piso por atleta, comportamento de hoje). Um
    preenchido e o outro nao e erro -- os dois ou nenhum."""
    ano_raw = (form.get('piso_importacao_ano', '') or '').strip()
    mes_raw = (form.get('piso_importacao_mes', '') or '').strip()
    if not ano_raw and not mes_raw:
        return None, None
    if not ano_raw or not mes_raw:
        raise ValueError('Informe mes e ano da fundacao do grupo, ou deixe os dois em branco.')
    try:
        ano = int(ano_raw)
        mes = int(mes_raw)
    except ValueError:
        raise ValueError('Mes/ano da fundacao do grupo invalidos.')
    if not 1 <= mes <= 12 or not 2000 <= ano <= 2100:
        raise ValueError('Mes/ano da fundacao do grupo invalidos.')
    return ano, mes


@admin_bp.route('/add_city', methods=['GET', 'POST'])
@login_required
@supremo_required  # 19/09/2026 (item 73): abrir, ativar e apagar cidade e do supremo
def add_city():
    """Adicionar uma nova cidade."""
    if request.method == 'POST':
        city_name = request.form.get('city_name', '').strip()
        if not city_name:
            flash('Nome da cidade não pode ser vazio.', 'error')
            return render_template('admin/add_city.html') # <<< CORRIGIDO: Retornar template para mostrar flash

        existing_city = Config.query.filter_by(cidade=city_name).first()
        if existing_city:
            flash(f"A cidade '{city_name}' já existe.", "warning")
            return render_template('admin/add_city.html') # <<< CORRIGIDO: Retornar template para mostrar flash

        try:
            piso_ano, piso_mes = _ler_piso_importacao(request.form)
        except ValueError as exc:
            flash(str(exc), 'error')
            return render_template('admin/add_city.html')

        new_city = Config(cidade=city_name, modificado_por_id=current_user.id,
                          piso_importacao_ano=piso_ano, piso_importacao_mes=piso_mes) # <<< NOVIDADE: Auditoria
        try:
            db.session.add(new_city)
            db.session.commit()
            flash(f"Cidade '{city_name}' adicionada com sucesso!", "success")
            logger.info(f"Admin {current_user.email} adicionou a cidade {city_name}.")
            return redirect(url_for('admin.manage_cities')) # <<< CORRIGIDO: Redirecionamento limpo
        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao adicionar cidade '{city_name}': {e}", exc_info=True)
            flash(f"Erro ao adicionar cidade: {e}", "error")

    return render_template('admin/add_city.html')

@admin_bp.route('/toggle_cidade_ativa/<string:city_name>', methods=['POST'])
@login_required
@master_or_aprovador_required
def toggle_cidade_ativa(city_name):
    """Liga/desliga a flag `ativa` da cidade (P-81, 20/08/2026).

    Cidade com atividade real ja e' tratada como ativa sem precisar deste
    botao (`consolidado.cidades_ativas`). Isto aqui serve para o caso que
    faltava: cidade nova, ainda sem nenhum link chegado, que mesmo assim deve
    poder mandar Wake-on-LAN e aparecer na inscricao publica -- e para
    desligar de vez uma cidade que nao deve mais operar.
    """
    city_config = Config.query.filter_by(cidade=city_name).first_or_404()
    city_config.ativa = not bool(city_config.ativa)
    city_config.modificado_por_id = current_user.id
    db.session.commit()
    flash(
        f"'{city_name}' agora está {'ATIVA' if city_config.ativa else 'INATIVA'}.",
        'success',
    )
    logger.info('Admin %s marcou %s como %s.', current_user.email, city_name,
               'ativa' if city_config.ativa else 'inativa')
    return redirect(url_for('admin.manage_cities'))


@admin_bp.route('/edit_city/<string:city_name>', methods=['GET', 'POST'])
@login_required
@supremo_required  # 19/09/2026 (item 73): abrir, ativar e apagar cidade e do supremo
def edit_city(city_name):
    """Editar configurações de uma cidade existente."""
    city_config = Config.query.filter_by(cidade=city_name).first_or_404()
    
    if request.method == 'POST':
        try:
            city_config.meta_corrida_caminhada = int(request.form.get('meta_corrida_caminhada', city_config.meta_corrida_caminhada))
            city_config.meta_ciclismo = int(request.form.get('meta_ciclismo', city_config.meta_ciclismo))
            # Patrocinador saiu daqui: pertence a (cidade, ano, mes) e se
            # edita em Ranking > Patrocinadores.
            city_config.chave_pix = request.form.get('chave_pix', city_config.chave_pix).strip()

            def _numero_configurado(nome, atual, permite_vazio=False):
                valor = (request.form.get(nome, '') or '').strip().replace(',', '.')
                if not valor and permite_vazio:
                    return None
                if not valor:
                    return atual
                numero = float(valor)
                if numero <= 0:
                    raise ValueError(f'{nome} deve ser maior que zero.')
                return numero

            city_config.pace_max_corrida_curta = _numero_configurado(
                'pace_max_corrida_curta', city_config.pace_max_corrida_curta or 4.167
            )
            city_config.pace_max_corrida_longa = _numero_configurado(
                'pace_max_corrida_longa', city_config.pace_max_corrida_longa or 5.0
            )
            city_config.velocidade_min_ciclismo = _numero_configurado(
                'velocidade_min_ciclismo', city_config.velocidade_min_ciclismo, permite_vazio=True
            )
            city_config.velocidade_max_ciclismo = _numero_configurado(
                'velocidade_max_ciclismo', city_config.velocidade_max_ciclismo or 45.0
            )
            if (
                city_config.velocidade_min_ciclismo is not None and
                city_config.velocidade_min_ciclismo >= city_config.velocidade_max_ciclismo
            ):
                raise ValueError('A velocidade minima do ciclismo deve ser menor que a maxima.')
            city_config.piso_importacao_ano, city_config.piso_importacao_mes = \
                _ler_piso_importacao(request.form)
            city_config.modificado_por_id = current_user.id # <<< NOVIDADE: Auditoria

            db.session.commit()
            flash(f"Configurações da cidade '{city_name}' atualizadas com sucesso!", "success")
            logger.info(f"Admin {current_user.email} editou a cidade {city_name}.")
            return redirect(url_for('admin.manage_cities')) # <<< CORRIGIDO: Redirecionamento limpo
        except Exception as e:
            db.session.rollback()
            logger.error(f"Erro ao editar cidade '{city_name}': {e}", exc_info=True)
            flash(f"Erro ao editar cidade: {e}", "error")

    return render_template('admin/edit_city.html', city_config=city_config)

@admin_bp.route('/delete_city/<string:city_name>', methods=['GET', 'POST'])
@login_required
@supremo_required
def delete_city(city_name):
    """Apagar uma cidade inteira — com backup verificado antes.

    Decisao do operador (24/07): o botao NAO some para cidade com dados; a
    protecao e o backup garantido, testado antes de qualquer DELETE. Se o
    backup nao abrir, nada e apagado.
    """
    config = Config.query.filter_by(cidade=city_name).first_or_404()

    atividades = Activity.query.filter_by(cidade=city_name).count()
    usuarios = Usuario.query.filter_by(cidade=city_name).all()
    bloqueio = None
    supremos_aqui = [u for u in usuarios if u.is_supremo]
    if supremos_aqui and Usuario.query.filter_by(tipo=TIPO_SUPREMO).count() <= len(supremos_aqui):
        bloqueio = ('O único master supremo do sistema pertence a esta cidade. '
                    'Transfira-o para outra cidade antes de apagar esta.')

    if request.method == 'POST' and not bloqueio:
        if (request.form.get('confirmo') or '') != 'sim':
            flash('Marque a confirmação para apagar a cidade.', 'warning')
            return redirect(url_for('admin.delete_city', city_name=city_name))

        ok, detalhe = backup_antes_de_apagar(f'cidade-{city_name}')
        if not ok:
            flash(f'Exclusão cancelada: não consegui gerar um backup confiável ({detalhe}).', 'error')
            return redirect(url_for('admin.delete_city', city_name=city_name))

        try:
            apagadas = _apagar_cidade(city_name)
            db.session.commit()
            from app import secretstore
            try:
                secretstore.delete_scope(city_name)
            except Exception as exc:
                logger.warning('Cofre da cidade %s nao pode ser limpo: %s', city_name, exc)
            logger.warning('Cidade %s apagada por %s. Backup: %s. Linhas: %s',
                           city_name, current_user.email, detalhe, apagadas)
            flash(f"Cidade '{city_name}' apagada. Backup completo salvo em {detalhe}.", 'success')
            return redirect(url_for('admin.manage_cities'))
        except Exception as exc:
            db.session.rollback()
            logger.error('Erro ao apagar cidade %s: %s', city_name, exc, exc_info=True)
            flash(f'Erro ao apagar a cidade: {exc}', 'error')
            return redirect(url_for('admin.delete_city', city_name=city_name))

    return render_template('admin/delete_city.html', config=config,
                           atividades=atividades, usuarios=usuarios,
                           bloqueio=bloqueio)


# Tabelas com coluna `cidade` que precisam sumir junto com a cidade. Listadas
# explicitamente para nunca apagar por engano algo que nao e da cidade.
_TABELAS_POR_CIDADE = (
    'activities', 'processed_files', 'monthly_backup', 'patrocinadores_mensais',
    'short_links', 'yearly_stats', 'feedback', 'prestacao_contas',
    'anticheat_override', 'avisos', 'medalhas_retiradas', 'medalhistas_mes',
    'local_retirada', 'patrocinador_catalogo', 'automacao_estado',
    'sessoes_whatsapp',
)


def _apagar_cidade(cidade):
    """Apaga a cidade e tudo que e dela. Devolve {tabela: linhas apagadas}."""
    from sqlalchemy import text as sa_text
    apagadas = {}
    inspetor = db.inspect(db.engine)
    existentes = set(inspetor.get_table_names())
    for tabela in _TABELAS_POR_CIDADE:
        if tabela not in existentes:
            continue
        try:
            res = db.session.execute(
                sa_text(f'DELETE FROM {tabela} WHERE cidade = :c'), {'c': cidade})
            apagadas[tabela] = res.rowcount
        except Exception as exc:
            logger.warning('Nao consegui limpar %s da cidade %s: %s', tabela, cidade, exc)
    Usuario.query.filter_by(cidade=cidade).delete(synchronize_session=False)
    Config.query.filter_by(cidade=cidade).delete(synchronize_session=False)
    return apagadas


@admin_bp.route('/comandos')
@login_required
@supremo_required
def comandos():
    """Comandos operacionais da maquina — só o dono da instalação.

    Esta pagina ensina a mexer no servidor inteiro (restart do uWSGI, docker,
    banco). Nao pertence ao admin de uma cidade.

    O host/usuario do SSH vem do cofre (instance/secrets.json, fora do Git
    -- mesmo mecanismo de app/secretstore.py usado para as outras chaves da
    automacao), nao mais hardcoded no template. .env nao serve pra isto:
    nada no projeto le .env (nem python-dotenv, nem os.environ direto em
    lugar nenhum que o carregue) -- e uwsgi.ini, que carregaria via `env =`,
    ESTA rastreado pelo Git, entao gravar la vazaria o mesmo jeito. O valor
    real so aparece para quem tem esta tela (supremo_required); o
    codigo-fonte, que pode virar publico
    (docs/planos/2026-08-21_F_github-portfolio_SONNET-medio.md), nao leva
    mais o endereco real do servidor.
    """
    from app import secretstore
    return render_template(
        'admin/comandos.html',
        ssh_hostname=secretstore.get(secretstore.SSH_HOSTNAME) or '',
        ssh_user=secretstore.get(secretstore.SSH_USER) or '',
    )


# ===== LIMPEZA DAS COPIAS DE PALMEIROPOLIS =====
# Em 19/07/2026 o historico de Palmeiropolis foi copiado para a cidade Sistema
# (marcador proprio em arquivo_origem). Onde a coleta propria do Sistema ja
# cobre o periodo, as copias aparecem em dobro na pagina publica do atleta.
# Esta tela apaga SOMENTE atividades com este marcador (nunca dados coletados
# de verdade) e SO no periodo que o master escolher — ano, mes ou data exata.
_COPIA_PALMEIROPOLIS_MARCADOR = 'copia_palmeiropolis_20260719'


def _intervalo_periodo(tipo, ano, mes, dia):
    """(inicio, fim, rotulo) para o filtro de limpeza. fim e exclusivo.

    tipo: 'ano' (ano inteiro), 'mes' (mes/ano), 'data' (dia exato).
    Levanta ValueError com mensagem amigavel quando os campos nao batem.
    """
    try:
        ano = int(ano)
    except (TypeError, ValueError):
        raise ValueError('Informe um ano valido.')
    if not 2000 <= ano <= 2100:
        raise ValueError('Informe um ano entre 2000 e 2100.')

    if tipo == 'ano':
        return datetime(ano, 1, 1), datetime(ano + 1, 1, 1), f'ano de {ano}'
    if tipo == 'mes':
        try:
            mes = int(mes)
        except (TypeError, ValueError):
            raise ValueError('Informe um mes valido.')
        if not 1 <= mes <= 12:
            raise ValueError('Informe um mes entre 1 e 12.')
        fim = datetime(ano + 1, 1, 1) if mes == 12 else datetime(ano, mes + 1, 1)
        return datetime(ano, mes, 1), fim, f'{mes:02d}/{ano}'
    if tipo == 'data':
        try:
            dia = int(dia)
            inicio = datetime(ano, int(mes), dia)
        except (TypeError, ValueError):
            raise ValueError('Informe uma data valida (ano, mes e dia).')
        return inicio, inicio + timedelta(days=1), inicio.strftime('%d/%m/%Y')
    raise ValueError('Escolha apagar por ano, por mes ou por data.')


def _query_copias(inicio=None, fim=None):
    q = Activity.query.filter(
        Activity.cidade == 'Sistema',
        Activity.arquivo_origem == _COPIA_PALMEIROPOLIS_MARCADOR,
    )
    if inicio is not None:
        q = q.filter(Activity.data_hora >= inicio)
    if fim is not None:
        q = q.filter(Activity.data_hora < fim)
    return q


def _executar_limpeza_copias(inicio, fim, rotulo):
    linhas = _query_copias(inicio, fim).all()
    if not linhas:
        flash(f'Nenhuma copia encontrada em {rotulo} para apagar.', 'info')
        return redirect(url_for('admin.limpeza_copias_palmeiropolis'))

    db_path = _PROJECT_ROOT / _SQLITE_DB_RELATIVE_PATH
    destino_backup = db_path.parent / (
        'movimente_se.pre_limpeza_copias_%s.db'
        % datetime.now().strftime('%Y%m%d_%H%M%S')
    )
    try:
        snapshot = _create_sqlite_snapshot(db_path)
        shutil.move(str(snapshot), str(destino_backup))
    except Exception as exc:
        logger.error('Limpeza de copias abortada: falha no backup fisico: %s',
                     exc, exc_info=True)
        flash(f'O backup fisico falhou ({exc}). NADA foi apagado.', 'danger')
        return redirect(url_for('admin.limpeza_copias_palmeiropolis'))

    ids = [a.id for a in linhas]
    periodos = {(a.data_hora.year, a.data_hora.month) for a in linhas if a.data_hora}
    try:
        # Flags que apontem para copias (hoje: zero) caem junto, sem orfaos.
        apagadas = 0
        for pos in range(0, len(ids), 500):
            lote = ids[pos:pos + 500]
            FlagAnticheat.query.filter(
                FlagAnticheat.atividade_id.in_(lote)
            ).delete(synchronize_session=False)
            apagadas += Activity.query.filter(
                Activity.id.in_(lote)
            ).delete(synchronize_session=False)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error('Erro ao apagar copias (%s): %s', rotulo, exc, exc_info=True)
        flash(f'Erro ao apagar: {exc}. Backup preservado em instance/{destino_backup.name}.',
              'danger')
        return redirect(url_for('admin.limpeza_copias_palmeiropolis'))

    from app.blueprints.upload import _invalidar_consolidacao
    _invalidar_consolidacao('Sistema', periodos)
    logger.info('Limpeza de copias (%s): %s atividades apagadas por %s (backup %s)',
                rotulo, apagadas, current_user.email, destino_backup.name)
    flash(f'{apagadas} copia(s) de {rotulo} apagada(s). Backup fisico: '
          f'instance/{destino_backup.name}. Rankings de {len(periodos)} mes(es) '
          'foram atualizados.', 'success')
    return redirect(url_for('admin.limpeza_copias_palmeiropolis'))


@admin_bp.route('/manutencao/copias-palmeiropolis', methods=['GET', 'POST'])
@login_required
@supremo_required
def limpeza_copias_palmeiropolis():
    """Previa e exclusao (com backup) das copias de Palmeiropolis, por periodo."""
    if request.method == 'POST':
        erro = _validate_admin_csrf()
        if erro:
            flash('Token CSRF invalido. Recarregue a pagina e tente novamente.', 'danger')
            return redirect(url_for('admin.limpeza_copias_palmeiropolis'))
        try:
            inicio, fim, rotulo = _intervalo_periodo(
                request.form.get('tipo_periodo'),
                request.form.get('ano'),
                request.form.get('mes'),
                request.form.get('dia'),
            )
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('admin.limpeza_copias_palmeiropolis'))
        return _executar_limpeza_copias(inicio, fim, rotulo)

    # Prévia geral por ano/mes de TODAS as copias que ainda existem, para o
    # master ver o que ha antes de escolher o periodo a apagar.
    por_mes = {}
    total_km = 0.0
    total_qtd = 0
    anos_disponiveis = set()
    for atividade in _query_copias().all():
        if atividade.data_hora:
            chave = (atividade.data_hora.year, atividade.data_hora.month)
            anos_disponiveis.add(atividade.data_hora.year)
        else:
            chave = (0, 0)
        item = por_mes.setdefault(chave, {'qtd': 0, 'km': 0.0})
        item['qtd'] += 1
        item['km'] += atividade.distancia or 0
        total_qtd += 1
        total_km += atividade.distancia or 0
    resumo = [
        {
            'label': f'{mes:02d}/{ano}' if ano else 'Sem data',
            'qtd': dados['qtd'],
            'km': dados['km'],
        }
        for (ano, mes), dados in sorted(por_mes.items())
    ]
    return render_template(
        'admin/limpeza_copias.html',
        marcador=_COPIA_PALMEIROPOLIS_MARCADOR,
        resumo=resumo,
        total_qtd=total_qtd,
        total_km=total_km,
        anos_disponiveis=sorted(anos_disponiveis, reverse=True),
        ano_atual=datetime.now().year,
    )


# ===== ADMIN REVIEW PANEL FOR ANTICHEAT FLAGS =====

@admin_bp.route('/reviews')
@login_required
@admin_required
def reviews():
    """Lista flags por status, com busca por atleta e resumo por categoria."""
    from app.models import FlagAnticheat, Atleta, Activity
    from sqlalchemy import func

    tab = request.args.get('tab', 'pendentes')
    q = (request.args.get('q', '') or '').strip()
    TAB_STATUS = {
        'pendentes':  ['em_revisao'],
        'abertas':    ['aberta'],
        'aprovadas':  ['aprovada'],
        'rejeitadas': ['rejeitada'],
    }
    statuses = TAB_STATUS.get(tab, ['em_revisao'])
    base_q = (
        FlagAnticheat.query
        .join(Activity, FlagAnticheat.atividade_id == Activity.id)
        .filter(Activity.cidade == current_user.cidade_ativa)
    )
    counts = {
        'pendentes':  base_q.filter(FlagAnticheat.status == 'em_revisao').count(),
        'abertas':    base_q.filter(FlagAnticheat.status == 'aberta').count(),
        'aprovadas':  base_q.filter(FlagAnticheat.status == 'aprovada').count(),
        'rejeitadas': base_q.filter(FlagAnticheat.status == 'rejeitada').count(),
    }

    flags_q = base_q.filter(FlagAnticheat.status.in_(statuses))
    if q:
        flags_q = (
            flags_q.join(Atleta, FlagAnticheat.atleta_id == Atleta.id)
            .filter(Atleta.nome_atual.ilike('%' + q + '%'))
        )
    flags = flags_q.order_by(FlagAnticheat.criada_em.desc()).all()

    atleta_ids = list({f.atleta_id for f in flags})
    atletas = {a.id: a for a in Atleta.query.filter(Atleta.id.in_(atleta_ids)).all()}

    # Resumo dos pendentes (em_revisao + aberta) por categoria e por atleta.
    pend_q = base_q.filter(FlagAnticheat.status.in_(['em_revisao', 'aberta']))
    resumo_tipos = sorted(
        pend_q.with_entities(
            FlagAnticheat.tipo_flag, func.count(FlagAnticheat.id)
        ).group_by(FlagAnticheat.tipo_flag).all(),
        key=lambda linha: linha[1], reverse=True,
    )
    atleta_rows = (
        pend_q.with_entities(
            FlagAnticheat.atleta_id, func.count(FlagAnticheat.id)
        ).group_by(FlagAnticheat.atleta_id)
        .order_by(func.count(FlagAnticheat.id).desc())
        .limit(12).all()
    )
    resumo_atleta_ids = [aid for aid, _ in atleta_rows]
    nomes_atletas = {
        a.id: a for a in Atleta.query.filter(Atleta.id.in_(resumo_atleta_ids)).all()
    } if resumo_atleta_ids else {}
    resumo_atletas = [
        {'atleta': nomes_atletas.get(aid), 'atleta_id': aid, 'total': total}
        for aid, total in atleta_rows
    ]

    from app.blueprints.athlete import gerar_link_revisao_publica
    review_links = {
        flag.id: gerar_link_revisao_publica(flag)
        for flag in flags
        if flag.status == 'aberta'
    }
    return render_template('admin/reviews.html',
                           flags=flags, atletas=atletas, tab=tab, counts=counts,
                           review_links=review_links, q=q,
                           resumo_tipos=resumo_tipos, resumo_atletas=resumo_atletas)


@admin_bp.route('/feedbacks')
@login_required
@admin_required
def feedbacks():
    """Lista os feedbacks publicos da cidade do usuario, filtrados por status."""
    cidade = current_user.cidade_ativa
    tab = request.args.get('tab', 'novas')
    TAB_STATUS = {
        'novas':      'nova',
        'lidas':      'lida',
        'resolvidas': 'resolvida',
    }
    status = TAB_STATUS.get(tab, 'nova')

    base_q = Feedback.query.filter_by(cidade=cidade)
    counts = {
        'novas':      base_q.filter_by(status='nova').count(),
        'lidas':      base_q.filter_by(status='lida').count(),
        'resolvidas': base_q.filter_by(status='resolvida').count(),
    }
    itens = (
        base_q.filter_by(status=status)
        .order_by(Feedback.criado_em.desc())
        .all()
    )
    TIPO_LABEL = {
        'revisao_ranking': 'Revisao do ranking',
        'sugestao':        'Sugestao',
        'reclamacao':      'Reclamacao',
    }
    return render_template(
        'admin/feedbacks.html',
        itens=itens, tab=tab, counts=counts, tipo_label=TIPO_LABEL,
    )


@admin_bp.route('/feedbacks/<int:fb_id>/status', methods=['POST'])
@login_required
@admin_required
def feedback_status(fb_id):
    """Atualiza o status de um feedback (lida/resolvida/nova)."""
    fb = Feedback.query.get_or_404(fb_id)
    if fb.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)
    novo = (request.form.get('status') or '').strip()
    if novo not in ('nova', 'lida', 'resolvida'):
        flash('Status invalido.', 'warning')
        return redirect(request.referrer or url_for('admin.feedbacks'))
    fb.status = novo
    if novo == 'resolvida':
        fb.resolvido_em = datetime.now()
        fb.resolvido_por_id = current_user.id
    else:
        fb.resolvido_em = None
        fb.resolvido_por_id = None
    db.session.commit()
    flash('Feedback atualizado.', 'success')
    return redirect(request.referrer or url_for('admin.feedbacks', tab=request.args.get('tab', 'novas')))


@admin_bp.route('/reviews/<int:flag_id>/evidencia', methods=['POST'])
@login_required
@admin_required
def upload_evidencia(flag_id):
    """Upload de evidência (admin) para uma flag anticheat."""
    import os
    from werkzeug.utils import secure_filename
    from app.models import FlagAnticheat, EvidenciaRevisao

    flag = FlagAnticheat.query.get_or_404(flag_id)
    arquivo = request.files.get('evidencia')
    if not arquivo or arquivo.filename == '':
        flash('Nenhum arquivo selecionado.', 'warning')
        return voltar('admin.reviews', tab=request.form.get('tab', 'pendentes'))

    TIPOS_PERMITIDOS = {'image/jpeg', 'image/png', 'image/gif', 'image/webp',
                        'application/pdf', 'application/msword',
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                        'text/plain'}
    MAX_SIZE = 10 * 1024 * 1024  # 10 MB

    mime = arquivo.content_type or ''
    if mime not in TIPOS_PERMITIDOS:
        flash('Tipo de arquivo não permitido. Use imagem, PDF ou DOCX.', 'error')
        return voltar('admin.reviews', tab=request.form.get('tab', 'pendentes'))

    arquivo.seek(0, 2)
    size = arquivo.tell()
    arquivo.seek(0)
    if size > MAX_SIZE:
        flash('Arquivo muito grande. Máximo 10 MB.', 'error')
        return voltar('admin.reviews', tab=request.form.get('tab', 'pendentes'))

    pasta = os.path.join('/volume1/web/movimente-se/uploads/evidencias', str(flag_id))
    os.makedirs(pasta, exist_ok=True)
    nome_seguro = secure_filename(arquivo.filename)
    # Evita colisão de nomes
    import time
    nome_final = f"{int(time.time())}_{nome_seguro}"
    caminho = os.path.join(pasta, nome_final)
    arquivo.save(caminho)

    ev = EvidenciaRevisao(
        flag_id=flag_id,
        arquivo_nome=nome_seguro,
        arquivo_path=caminho,
        tipo_mime=mime,
        tamanho=size,
        enviado_por=current_user.email,
    )
    db.session.add(ev)
    db.session.commit()
    logger.info(f"Evidencia enviada por {current_user.email} para flag {flag_id}: {nome_seguro}")
    flash(f'Evidência "{nome_seguro}" anexada com sucesso.', 'success')
    return voltar('admin.reviews', tab=request.form.get('tab', 'pendentes'))


@admin_bp.route('/reviews/evidencia/<int:ev_id>')
@login_required
@admin_required
def ver_evidencia(ev_id):
    """Serve o arquivo de evidência para download/visualização."""
    import os
    from flask import send_file
    from app.models import EvidenciaRevisao
    ev = EvidenciaRevisao.query.get_or_404(ev_id)
    if not os.path.exists(ev.arquivo_path):
        flash('Arquivo não encontrado no servidor.', 'error')
        return redirect(url_for('admin.reviews'))
    return send_file(ev.arquivo_path, download_name=ev.arquivo_nome, as_attachment=False)


def _aprovar_flag(flag, contar_anual, velocidade_confiavel, observacao=''):
    """Aprova UMA flag. O miolo, sem Flask nem commit.

    Existe porque ha dois botoes que aprovam -- o de uma flag e o de aprovar em
    massa (09/09/2026, pedido do dono) -- e duas copias desta regra divergiriam
    na primeira mudanca. Quem chama e' quem decide o `commit`.

    `contar_anual` e `velocidade_confiavel` sao booleanos JA resolvidos por
    quem chama: aqui nao se le formulario.
    """
    from datetime import datetime
    from app.blueprints.anticheat import (
        atividade_pode_voltar_ao_ranking,
        recalcular_controle_disciplinar,
    )
    flag.status = 'aprovada'
    flag.resolvida_em = datetime.now()
    flag.resolvida_por_id = current_user.id
    flag.observacao_admin = observacao

    # A atividade so volta a contar se nao houver outra flag pendente ou
    # rejeitada nela. Isso evita restaurar uma atividade com duas flags quando
    # apenas uma delas foi aprovada.
    if not flag.atividade:
        return False
    flag.atividade.excluir_do_anual = not contar_anual
    # Regra 14: flag nao tira km, mas velocidade anomala fica fora da tabela
    # publica ate um admin dizer que e' real.
    if velocidade_confiavel:
        flag.atividade.velocidade_confiavel = True
    voltou = False
    if atividade_pode_voltar_ao_ranking(flag.atividade):
        flag.atividade.status = 'ativa'
        voltou = True
        logger.info('Atividade %s restaurada para ativa apos aprovacao',
                    flag.atividade.id_atividade)
    if flag.atividade.data_hora:
        recalcular_controle_disciplinar(
            flag.atleta_id, flag.atividade.data_hora.strftime('%Y-%m'))
    return voltou


@admin_bp.route('/reviews/aprovar-em-massa', methods=['POST'])
@login_required
@admin_required
def aprovar_revisoes_em_massa():
    """Aprova de uma vez todas as flags marcadas na tela.

    POR QUE EXISTE (09/09/2026, pedido do dono): com mais de mil flags em
    aberto, aprovar uma por uma nao e' trabalho que alguem faca -- e uma tela
    que nao da conta do volume dela vira tela que ninguem abre. O padrao das
    flags e' repetitivo (o mesmo atleta rapido, a mesma subida), e o que o dono
    precisa e' varrer o obvio para sobrar tempo no que e' duvida de verdade.

    REJEITAR EM MASSA passou a existir em 01/10/2026, a pedido do dono
    (`rejeitar_revisoes_em_massa`). Aprovar continua sem justificativa
    obrigatoria: erra para o lado de contar o km (regra 14).

    So aprova flags DA CIDADE ATIVA e que ainda nao foram resolvidas -- id
    vindo de formulario nao autoriza nada por si.
    """
    from app.models import FlagAnticheat, Activity

    tab = request.form.get('tab', 'pendentes')
    brutos = request.form.getlist('flag_ids')
    ids = []
    for b in brutos:
        try:
            ids.append(int(b))
        except (TypeError, ValueError):
            continue
    if not ids:
        flash('Nenhuma revisão marcada. Marque as que você quer aprovar.',
              'warning')
        return voltar('admin.reviews', tab=tab)

    # A CIDADE E O STATUS SAO CONFERIDOS AQUI, no servidor (regra 26). O
    # formulario diz o que o dono marcou, nao o que ele pode.
    flags = (FlagAnticheat.query
             .join(Activity, FlagAnticheat.atividade_id == Activity.id)
             .filter(Activity.cidade == current_user.cidade_ativa,
                     FlagAnticheat.id.in_(ids),
                     FlagAnticheat.status.in_(['aberta', 'em_revisao']))
             .all())
    if not flags:
        flash('Nenhuma das revisões marcadas está aberta nesta cidade.',
              'warning')
        return voltar('admin.reviews', tab=tab)

    contar_anual = request.form.get('contar_no_anual') is not None
    vel_confiavel = request.form.get('velocidade_confiavel') is not None
    observacao = (request.form.get('observacao', '') or '').strip() or         'Aprovada em lote pelo administrador.'

    voltaram = 0
    for flag in flags:
        try:
            if _aprovar_flag(flag, contar_anual, vel_confiavel, observacao):
                voltaram += 1
        except Exception as exc:
            logger.error('Aprovacao em massa: flag %s falhou (%s)',
                         flag.id, exc, exc_info=True)
    db.session.commit()
    logger.info('%s aprovou %d revisoes em massa (cidade %s)',
                current_user.email, len(flags), current_user.cidade_ativa)

    recado = '%d revisão aprovada' if len(flags) == 1 else '%d revisões aprovadas'
    extra = ''
    if voltaram:
        extra = (' %d atividade voltou ao ranking.' if voltaram == 1
                 else ' %d atividades voltaram ao ranking.') % voltaram
    ignoradas = len(ids) - len(flags)
    if ignoradas > 0:
        extra += ' %d já estavam resolvidas e ficaram como estão.' % ignoradas
    flash((recado % len(flags)) + '.' + extra, 'success')
    return voltar('admin.reviews', tab=tab)


@admin_bp.route('/reviews/<int:flag_id>/aprovar', methods=['POST'])
@login_required
@admin_required
def aprovar_revisao(flag_id):
    """Aprova UMA revisao: o km volta a contar e a flag fica resolvida.

    A regra mora em `_aprovar_flag`, compartilhada com o botao de aprovar em
    massa. Aqui fica so' o que e' de formulario: ler as caixas e responder.
    """
    from app.models import FlagAnticheat
    flag = FlagAnticheat.query.get_or_404(flag_id)

    # Caixa "Contar km no ranking anual": marcada por padrao. Desmarcar mantem
    # a atividade no ranking mensal e a tira do anual (punicao ao reincidente
    # sem constrangimento publico).
    contar_anual = request.form.get('contar_no_anual') is not None
    vel_confiavel = request.form.get('velocidade_confiavel') is not None
    _aprovar_flag(flag, contar_anual, vel_confiavel,
                  request.form.get('observacao', ''))
    db.session.commit()
    flash('Revisao aprovada. A atividade volta a contar no ranking.', 'success')
    return voltar('admin.reviews')


def _rejeitar_flag(flag, observacao):
    """Rejeita UMA flag: o km sai do ranking. Sem commit — quem chama decide.

    Compartilhada pelo botao de uma e pelo de rejeitar em massa (01/10/2026),
    para a regra morar num lugar so.
    """
    flag.status = 'rejeitada'
    flag.resolvida_em = datetime.now()
    flag.resolvida_por_id = current_user.id
    flag.observacao_admin = observacao
    # Mantem a atividade no banco, mas fora do ranking. Os contadores sao
    # recalculados por atividade suspeita para nao duplicar flags da mesma prova.
    if flag.atividade:
        flag.atividade.status = 'removida'
        if flag.atividade.data_hora:
            from app.blueprints.anticheat import recalcular_controle_disciplinar
            recalcular_controle_disciplinar(
                flag.atleta_id, flag.atividade.data_hora.strftime('%Y-%m')
            )


@admin_bp.route('/reviews/rejeitar-em-massa', methods=['POST'])
@login_required
@admin_required
def rejeitar_revisoes_em_massa():
    """Rejeita de uma vez as flags marcadas na tela (pedido do dono, 01/10/2026).

    Mesmas travas do aprovar em massa: so flags DA CIDADE ATIVA e ainda
    abertas — id vindo de formulario nao autoriza nada por si. Justificativa
    NAO e obrigatoria (decisao do dono): sem texto, grava o padrao. A tela
    pede confirmacao com a contagem antes, porque rejeitar tira km.
    """
    from app.models import FlagAnticheat, Activity

    ids = []
    for b in request.form.getlist('flag_ids'):
        try:
            ids.append(int(b))
        except (TypeError, ValueError):
            continue
    if not ids:
        flash('Nenhuma revisão marcada. Marque as que você quer rejeitar.',
              'warning')
        return voltar('admin.reviews')
    flags = (FlagAnticheat.query
             .join(Activity, FlagAnticheat.atividade_id == Activity.id)
             .filter(Activity.cidade == current_user.cidade_ativa,
                     FlagAnticheat.id.in_(ids),
                     FlagAnticheat.status.in_(['aberta', 'em_revisao']))
             .all())
    if not flags:
        flash('Nenhuma das revisões marcadas está aberta nesta cidade.',
              'warning')
        return voltar('admin.reviews')

    observacao = ((request.form.get('observacao_rejeicao') or '').strip()
                  or 'Rejeitada em lote pelo administrador.')
    for flag in flags:
        try:
            _rejeitar_flag(flag, observacao)
        except Exception as exc:
            logger.error('Rejeicao em massa: flag %s falhou (%s)',
                         flag.id, exc, exc_info=True)
    db.session.commit()
    logger.info('%s rejeitou %d revisoes em massa (cidade %s)',
                current_user.email, len(flags), current_user.cidade_ativa)
    recado = ('%d revisão rejeitada' if len(flags) == 1
              else '%d revisões rejeitadas') % len(flags)
    ignoradas = len(ids) - len(flags)
    extra = (' %d já estavam resolvidas e ficaram como estão.' % ignoradas
             ) if ignoradas > 0 else ''
    flash(recado + '. O km dessas atividades não conta no ranking.' + extra,
          'warning')
    return voltar('admin.reviews')


@admin_bp.route('/reviews/<int:flag_id>/rejeitar', methods=['POST'])
@login_required
@admin_required
def rejeitar_revisao(flag_id):
    """Reject a review request - activity km does NOT count."""
    from app.models import FlagAnticheat, ControleDisciplinar
    flag = FlagAnticheat.query.get_or_404(flag_id)

    observacao = request.form.get('observacao', '').strip()
    if not observacao:
        flash('Informe uma justificativa para rejeitar a revisão.', 'danger')
        return voltar('admin.reviews')

    _rejeitar_flag(flag, observacao)
    db.session.commit()
    flash('Revisao rejeitada. A atividade nao contabilizara km no ranking.', 'warning')
    return voltar('admin.reviews')

@admin_bp.route('/reviews/<int:flag_id>/reabrir', methods=['POST'])
@login_required
@master_required
def reabrir_revisao(flag_id):
    from app.models import FlagAnticheat
    from datetime import datetime
    flag = FlagAnticheat.query.get_or_404(flag_id)
    flag.status = 'em_revisao'
    flag.resolvida_em = None
    flag.solicitado_revisao_em = datetime.now()
    db.session.commit()
    flash('Flag reaberta para revisao.', 'info')
    return voltar('admin.reviews', tab='pendentes')


@admin_bp.route('/reviews/<int:flag_id>/recurso', methods=['POST'])
@login_required
@admin_required
def registrar_recurso(flag_id):
    from app.models import FlagAnticheat
    from datetime import datetime
    flag = FlagAnticheat.query.get_or_404(flag_id)
    obs = request.form.get('observacao', '').strip()
    if not obs:
        flash('Informe a justificativa do recurso.', 'danger')
        return voltar('admin.reviews', tab='rejeitadas')
    recursos = getattr(flag, 'recursos_utilizados', 0) or 0
    max_rec = getattr(flag, 'max_recursos', 2) or 2
    if recursos >= max_rec and not current_user.is_master:
        flash('Recursos esgotados. Apenas ADM Master pode reabrir.', 'danger')
        return voltar('admin.reviews', tab='rejeitadas')
    flag.recursos_utilizados = recursos + 1
    flag.status = 'em_revisao'
    flag.observacao_admin = '[Recurso {}/{}] {}'.format(recursos+1, max_rec, obs)
    flag.solicitado_revisao_em = datetime.now()
    db.session.commit()
    flash('Recurso {} registrado. Flag voltou para revisao.'.format(recursos+1), 'info')
    return voltar('admin.reviews', tab='pendentes')


@admin_bp.route('/anticheat', methods=['GET', 'POST'])
@login_required
@admin_required
def anticheat_config():
    """Parametros do anti-cheat: limites da cidade, tolerancia e overrides por atleta."""
    from app.models import AnticheatOverride
    from app.blueprints.anticheat import LIMITES

    cidade = current_user.cidade_ativa
    config = Config.query.filter_by(cidade=cidade).first()

    def _num(nome):
        """Le um numero positivo do form; retorna None se vazio/invalido."""
        valor = (request.form.get(nome, '') or '').strip().replace(',', '.')
        if not valor:
            return None
        try:
            numero = float(valor)
        except ValueError:
            return None
        return numero if numero > 0 else None

    if request.method == 'POST':
        acao = request.form.get('acao', 'limites')

        if acao == 'limites':
            if not config:
                flash('Configuracao da cidade nao encontrada.', 'danger')
                return voltar('admin.anticheat_config')
            pc = _num('pace_max_corrida_curta')
            pl = _num('pace_max_corrida_longa')
            vmax = _num('velocidade_max_ciclismo')
            # Pace e velocidade maxima nunca ficam vazios: campo vazio mantem o atual.
            if pc is not None:
                config.pace_max_corrida_curta = pc
            if pl is not None:
                config.pace_max_corrida_longa = pl
            if vmax is not None:
                config.velocidade_max_ciclismo = vmax
            # Velocidade minima pode ser limpa (vazio = sem minimo).
            config.velocidade_min_ciclismo = _num('velocidade_min_ciclismo')
            if (config.velocidade_min_ciclismo is not None
                    and config.velocidade_max_ciclismo is not None
                    and config.velocidade_min_ciclismo >= config.velocidade_max_ciclismo):
                flash('A velocidade minima do ciclismo deve ser menor que a maxima.', 'danger')
                return voltar('admin.anticheat_config')
            try:
                tol_int = int((request.form.get('tolerancia_flags', '') or '').strip())
                if tol_int >= 1:
                    config.tolerancia_flags = tol_int
            except ValueError:
                pass
            config.modificado_por_id = current_user.id
            db.session.commit()
            flash('Parametros do anti-cheat atualizados.', 'success')
            return voltar('admin.anticheat_config')

        if acao == 'override_salvar':
            strava_id = (request.form.get('strava_id', '') or '').strip()
            if not strava_id.isdigit():
                flash('Informe um Strava ID valido (apenas numeros).', 'danger')
                return voltar('admin.anticheat_config')
            ov = AnticheatOverride.query.filter_by(strava_id=strava_id).first()
            if not ov:
                ov = AnticheatOverride(strava_id=strava_id, criado_por=current_user.id)
                db.session.add(ov)
            atleta = Atleta.query.filter_by(strava_id=strava_id).first()
            nome = (request.form.get('nome_atleta', '') or '').strip()
            ov.nome_atleta = nome or (atleta.nome_atual if atleta else None)
            ov.cidade = cidade
            ov.pace_max_corrida_curta = _num('ov_pace_max_corrida_curta')
            ov.pace_max_corrida_longa = _num('ov_pace_max_corrida_longa')
            ov.velocidade_min_ciclismo = _num('ov_velocidade_min_ciclismo')
            ov.velocidade_max_ciclismo = _num('ov_velocidade_max_ciclismo')
            obs = (request.form.get('observacao', '') or '').strip()
            ov.observacao = obs[:255] or None
            db.session.commit()
            flash('Limite individual salvo para o atleta.', 'success')
            return voltar('admin.anticheat_config')

        if acao == 'override_remover':
            ov_id = request.form.get('override_id')
            ov = AnticheatOverride.query.get(ov_id) if ov_id else None
            if ov and (current_user.is_supremo or ov.cidade == cidade):
                db.session.delete(ov)
                db.session.commit()
                flash('Limite individual removido.', 'info')
            return voltar('admin.anticheat_config')

        return voltar('admin.anticheat_config')

    # GET
    if current_user.is_supremo:
        overrides = (AnticheatOverride.query
                     .order_by(AnticheatOverride.cidade, AnticheatOverride.nome_atleta).all())
        atletas_lista = Atleta.query.order_by(Atleta.nome_atual).all()
    else:
        overrides = (AnticheatOverride.query.filter_by(cidade=cidade)
                     .order_by(AnticheatOverride.nome_atleta).all())
        strava_ids = db.session.query(Activity.id_usuario_strava).filter_by(
            cidade=cidade
        ).distinct().subquery()
        atletas_lista = (Atleta.query.filter(Atleta.strava_id.in_(strava_ids))
                         .order_by(Atleta.nome_atual).all())

    # O PADRÃO DE CADA ATLETA, PARA O LIMITE NÃO SER PALPITE (19/09/2026,
    # pedido do dono — item 75). São ~1.100 flags de velocidade abertas: ver
    # uma a uma não é trabalho de gente. Aqui cada atleta aparece com quantas
    # flags tem e em que faixa de velocidade elas caem — quem corre sempre a
    # 14 km/h não é fraude, é o padrão dele, e o limite individual resolve as
    # próximas. Só LEITURA: ajustar o limite continua sendo o formulário
    # abaixo, e aprovar as flags continua na tela de Revisões.
    # O PADRÃO DE CADA ATLETA, PARA O LIMITE NÃO SER PALPITE (item 75).
    try:
        _cidade_filtro = None if current_user.is_supremo else (
            cidade if not current_user.is_master else current_user.cidade_ativa)
        resumo_velocidade = _resumo_flags_velocidade(_cidade_filtro, overrides)
    except Exception as exc:
        logger.warning('anticheat: nao consegui resumir as flags de velocidade: %s', exc)
        resumo_velocidade = []

    return render_template('admin/anticheat.html',
                           config=config, overrides=overrides,
                           resumo_velocidade=resumo_velocidade,
                           atletas_lista=atletas_lista, limites_default=LIMITES)


def _resumo_flags_velocidade(cidade_filtro, overrides, limite_linhas=80):
    """Por atleta e modalidade: quantas flags de velocidade em aberto e em que
    faixa elas caem (19/09/2026, item 75 — pedido do dono).

    São ~1.100 flags abertas: ver uma a uma não é trabalho de gente. Quem
    repete a mesma faixa em dezenas de atividades tem um PADRÃO, e o limite
    individual (abaixo, na mesma tela) resolve as próximas. Só leitura.

    `cidade_filtro` None = todas as cidades (só o supremo chega assim).
    """
    from app.models import FlagAnticheat as _Flag
    from sqlalchemy import func
    q = (db.session.query(
            Activity.id_usuario_strava.label('strava_id'),
            Activity.nome_usuario.label('nome'),
            Activity.cidade.label('cidade'),
            Activity.modalidade.label('modalidade'),
            func.count(_Flag.id).label('flags'),
            func.min(Activity.velocidade_media).label('vel_min'),
            func.avg(Activity.velocidade_media).label('vel_media'),
            func.max(Activity.velocidade_media).label('vel_max'),
            func.max(Activity.data_hora).label('ultima'))
         .join(Activity, _Flag.atividade_id == Activity.id)
         .filter(_Flag.tipo_flag.in_(('VELOCIDADE_ANORMAL', 'TEMPO_IMPOSSIVEL')),
                 _Flag.status.in_(('aberta', 'em_revisao')),
                 Activity.velocidade_media.isnot(None)))
    if cidade_filtro:
        q = q.filter(Activity.cidade == cidade_filtro)
    limites = {o.strava_id: o for o in (overrides or ())}
    saida = []
    for r in (q.group_by(Activity.id_usuario_strava, Activity.nome_usuario,
                         Activity.cidade, Activity.modalidade)
               .order_by(func.count(_Flag.id).desc()).limit(limite_linhas).all()):
        saida.append({
            'strava_id': r.strava_id, 'nome': r.nome, 'cidade': r.cidade,
            'modalidade': r.modalidade, 'flags': r.flags,
            'vel_min': round(r.vel_min or 0, 1),
            'vel_media': round(r.vel_media or 0, 1),
            'vel_max': round(r.vel_max or 0, 1),
            'ultima': r.ultima.strftime('%d/%m/%Y') if r.ultima else '',
            'tem_limite': bool(limites.get(str(r.strava_id)))})
    return saida


@admin_bp.route('/cidades/renomear', methods=['GET', 'POST'])
@login_required
@master_required
def renomear_cidade():
    """Renomeia uma cidade em todas as tabelas de uma vez, com confirmacao.

    O nome da cidade e a chave que liga atividades, usuarios, rankings e links
    publicos. Fazer isso pela aplicacao (e nao por SQL na mao) garante que
    nenhuma tabela fique para tras e que a troca aconteca dentro de uma
    transacao, com o proprio processo que e dono do banco.
    """
    from app import cityrename

    if request.method == 'POST':
        origem = (request.form.get('cidade_origem') or '').strip()
        destino = (request.form.get('cidade_destino') or '').strip()
        # Confirmacao por caixa marcada (retypar o nome acentuado era a causa
        # mais comum de "nao renomeia": bastava um acento diferente).
        if (request.form.get('confirmar') or '') not in ('on', 'true', '1', 'sim'):
            flash('Marque a caixa de confirmacao antes de renomear.', 'warning')
            return redirect(url_for('admin.renomear_cidade'))
        try:
            resultado = cityrename.renomear(origem, destino, autor=current_user.email)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return redirect(url_for('admin.renomear_cidade'))
        except Exception as exc:
            logger.error('Erro ao renomear cidade: %s', exc, exc_info=True)
            flash(f'Erro ao renomear: {exc}. Nada foi alterado.', 'danger')
            return redirect(url_for('admin.renomear_cidade'))

        detalhe = ', '.join(f'{t}: {n}' for t, n in sorted(resultado['linhas'].items()))
        flash(f'Cidade "{origem}" renomeada para "{destino}". '
              f'{resultado["total"]} registro(s) atualizados ({detalhe}).', 'success')
        if resultado.get('aviso'):
            flash(resultado['aviso'], 'warning')
        if current_user.cidade_ativa == destino:
            flash('Sua propria cidade mudou de nome: saia e entre de novo para a '
                  'navegacao refletir a mudanca.', 'info')
        return redirect(url_for('admin.renomear_cidade'))

    cidades = []
    for nome in cityrename.cidades_existentes():
        cidades.append({'nome': nome, 'linhas': cityrename.contagem(nome)})
    return render_template('admin/renomear_cidade.html', cidades=cidades)


@admin_bp.route('/strava-sessao', methods=['GET', 'POST'])
@login_required
@supremo_required  # 19/09/2026 (item 73): cookie/token/endereco e do supremo
def strava_sessao():
    """Cookie de sessao do Strava usado na importacao por perfil de atleta.

    O valor fica no cofre em instance/ (fora do banco, fora do git e fora do
    backup) e nunca volta para a tela: a pagina mostra apenas metadados.
    """
    from app import secretstore
    from app.blueprints.processing import (
        STRAVA_SESSION_COOKIE_NAME, validate_strava_session_cookie,
    )

    escopo = current_user.cidade_ativa

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()

        if acao == 'salvar':
            valor = (request.form.get('cookie_valor') or '').strip()
            # Tolerancia com o que o usuario cola: "_strava4_session=abc; Path=/"
            # ou o par inteiro copiado do DevTools viram apenas o valor.
            if '=' in valor and valor.lower().startswith(STRAVA_SESSION_COOKIE_NAME.lower()):
                valor = valor.split('=', 1)[1]
            valor = valor.split(';', 1)[0].strip().strip('"').strip("'")
            if not valor:
                flash('Cole o valor do cookie antes de salvar.', 'warning')
                return voltar('admin.strava_sessao')
            if not secretstore.set_value(secretstore.STRAVA_SESSION, valor,
                                         scope=escopo, updated_by=current_user.email):
                flash('Nao foi possivel gravar o cookie. Verifique as permissoes da pasta instance.', 'danger')
                return voltar('admin.strava_sessao')
            ok, mensagem, _athlete_id = validate_strava_session_cookie(valor)
            secretstore.record_session_health(escopo, ok, mensagem)
            logger.info('Cookie do Strava atualizado por %s (valido=%s)', current_user.email, ok)
            flash(f'Cookie salvo. {mensagem}', 'success' if ok else 'warning')
            return voltar('admin.strava_sessao')

        if acao == 'remover':
            secretstore.delete(secretstore.STRAVA_SESSION, scope=escopo)
            flash('Cookie removido. A importacao por perfil ficara indisponivel ate configurar outro.', 'info')
            return voltar('admin.strava_sessao')

        if acao == 'testar':
            atual = secretstore.get(secretstore.STRAVA_SESSION, scope=escopo)
            if not atual:
                flash('Nenhum cookie configurado para testar.', 'warning')
                return voltar('admin.strava_sessao')
            ok, mensagem, _athlete_id = validate_strava_session_cookie(atual)
            secretstore.record_session_health(escopo, ok, mensagem)
            flash(mensagem, 'success' if ok else 'danger')
            return voltar('admin.strava_sessao')

        return voltar('admin.strava_sessao')

    dados = secretstore.info(secretstore.STRAVA_SESSION, scope=escopo)
    if dados.get('atualizado_em'):
        dados['atualizado_em_texto'] = datetime.fromtimestamp(
            dados['atualizado_em']
        ).strftime('%d/%m/%Y %H:%M')
    else:
        dados['atualizado_em_texto'] = ''
    saude = secretstore.session_health(escopo)
    if saude and saude.get('checked_at'):
        saude = dict(saude)
        saude['verificado_em_texto'] = datetime.fromtimestamp(
            saude['checked_at']
        ).strftime('%d/%m/%Y %H:%M')
    return render_template(
        'admin/strava_sessao.html',
        info=dados,
        saude=saude,
        cookie_nome=STRAVA_SESSION_COOKIE_NAME,
        escopo=escopo,
    )


# ===== SESSAO DO WHATSAPP (WAHA) =====

def _waha_config(escopo):
    """Endereco e chave do WAHA guardados no cofre.

    O WAHA roda no Dell (WSL ou Xpenology), nao aqui no DS215j — por isso o
    endereco e um IP da rede local, e nao localhost.
    """
    from app import secretstore
    url = (secretstore.get(secretstore.WAHA_URL, scope=escopo) or '').strip().rstrip('/')
    chave = (secretstore.get(secretstore.WAHA_API_KEY, scope=escopo) or '').strip()
    return url, chave


@admin_bp.route('/modo-teste', methods=['POST'])
@login_required
@master_required
def modo_teste():
    """Liga e desliga o MODO TESTE da cidade (P-27).

    LIGAR é livre; DESLIGAR pede confirmação na tela, porque é este clique que
    abre o grupo de 80 pessoas. Não existe despublicar (regra 16-C).

    O estado mora no cofre e a pipeline pergunta em `GET /api/config/envio`.
    Se ela não conseguir perguntar, mantém o ensaio — falha fechada.
    """
    from app import secretstore
    from app.telefone import normalizar

    escopo = current_user.cidade_ativa
    ligar = (request.form.get('ligar') or '') == 'sim'

    # O ADMIN DIGITA DDD + NÚMERO COM O 9; o `55` é nosso (regra 22-A: na
    # entrada o sistema conserta o que dá e nunca cobra formato do usuário).
    destino = normalizar(request.form.get('destino') or '') or ''

    if ligar:
        if len(destino) < 12:
            flash('Informe o número com DDD e o 9, por exemplo DD9XXXXXXXX. '
                  'O código do país eu coloco.', 'danger')
            return voltar('admin.dashboard')
        secretstore.set_value(secretstore.MODO_TESTE_DESTINO, destino,
                              scope=escopo, updated_by=current_user.email)
        # GRUPO DE TESTE (15/08/2026, pedido do dono). Mensagem de grupo
        # redirecionada para um NÚMERO não prova nada: menção com @ não marca
        # ninguém, e o texto some no meio das outras mensagens de teste. Com um
        # grupo só de teste (principal + suplente dentro), o ensaio vira igual
        # ao real. Em branco = comportamento antigo, tudo no número.
        grupo_teste = (request.form.get('grupo_teste') or '').strip()
        if grupo_teste and not grupo_teste.endswith('@g.us'):
            flash('O grupo de teste tem de terminar em "@g.us" — é o '
                  'identificador que o WhatsApp usa. O resto foi salvo.', 'warning')
        elif grupo_teste:
            secretstore.set_value(secretstore.MODO_TESTE_GRUPO, grupo_teste,
                                  scope=escopo, updated_by=current_user.email)
        elif request.form.get('grupo_teste') is not None:
            secretstore.delete(secretstore.MODO_TESTE_GRUPO, scope=escopo)
        secretstore.set_value(secretstore.MODO_TESTE, 'sim',
                              scope=escopo, updated_by=current_user.email)
        logger.warning('MODO TESTE LIGADO em %s por %s (destino %s, grupo %s)',
                       escopo, current_user.email, destino,
                       grupo_teste or '(sem grupo de teste)')
        flash('Modo teste LIGADO. Nada vai para o grupo real — o privado cai em '
              '%s%s.' % (destino, (' e o que é de grupo vai para o grupo de teste'
                                   if grupo_teste else '')), 'warning')
    else:
        secretstore.set_value(secretstore.MODO_TESTE, 'nao',
                              scope=escopo, updated_by=current_user.email)
        logger.warning('MODO TESTE DESLIGADO em %s por %s — as mensagens '
                       'voltam a ir para o GRUPO', escopo, current_user.email)
        flash('Modo teste DESLIGADO. A partir da próxima rodada as mensagens '
              'vão para o GRUPO.', 'success')
    return voltar('admin.dashboard')


@admin_bp.route('/manter-ligada', methods=['POST'])
@login_required
@master_required
def manter_ligada():
    """Liga/desliga a alavanca "manter ligada" (item 4.1-b do MVP de 17/08).

    Não desliga nem liga a máquina — só pede para o `Dormir.ps1` pular a
    parada limpa desta janela (`GET /api/maquina/pode-dormir`). Sem prazo:
    fica ligada até o próprio dono desligar a alavanca de volta.

    17/08/2026 (plano attach-tidy-globe, D3) — ESCOPO NAO E MAIS
    `cidade_ativa`. Medido: o painel gravava em `current_user.cidade_ativa` e
    `GET /api/maquina/pode-dormir` lia a cidade do TOKEN da automação — o
    master supremo troca de cidade pela tela, então marcar a alavanca em
    Minaçu/Teste não tinha nenhum efeito na máquina de Palmeirópolis, e a
    tela não avisava em qual cidade estava marcando. Só o master supremo
    chega aqui (é dele o computador, é dele a escolha — decisão do dono), e o
    valor vai para a(s) cidade(s) que TÊM automação de verdade (o mesmo
    critério que `api._candidatos_token()` usa para decidir quem pergunta),
    não para a cidade que ele está navegando no momento.
    """
    if not current_user.is_supremo:
        flash('Só o master supremo controla a máquina da automação.', 'danger')
        return voltar('admin.dashboard')

    from app import secretstore

    ligar = (request.form.get('ligar') or '') == 'sim'
    cidades_com_automacao = sorted({
        (escopo or '').strip()
        for escopo, _ in secretstore.entries(secretstore.AUTOMATION_TOKEN)
        if (escopo or '').strip()
    })
    if not cidades_com_automacao:
        flash('Nenhuma cidade tem token de automação cadastrado — não há '
              'máquina para manter ligada.', 'warning')
        return voltar('admin.dashboard')
    for escopo in cidades_com_automacao:
        secretstore.set_value(secretstore.MANTER_LIGADA, 'sim' if ligar else 'nao',
                              scope=escopo, updated_by=current_user.email)
    if ligar:
        flash('A máquina da automação vai ficar ligada — ela não vai fazer a '
              'parada limpa sozinha enquanto isto estiver marcado.', 'warning')
    else:
        flash('Desmarcado. A máquina volta a desligar sozinha no horário de costume.',
              'success')
    return voltar('admin.dashboard')


@admin_bp.route('/parametros-velocidade', methods=['GET', 'POST'])
@login_required
@admin_required
def parametros_velocidade():
    """A régua do que é plausível — por cidade, por modalidade e por distância.

    POR QUE ESTA TELA EXISTE (09/09/2026, pedido do dono)

    O antitrapaça só sabia dizer "acima de X é suspeito", com um X só para toda
    distância e para todo mundo. Isso marca gente honesta e deixa passar o que
    devia pegar. Com mais de mil flags em aberto, o dono não vai revisar uma a
    uma: ele precisa de uma régua que decida o óbvio e que ele controle.

    A PRÉVIA É O CORAÇÃO DA TELA. Antes de salvar, ele vê quantas atividades
    cada valor deixa de fora. É o que transforma "17 km/h parece razoável" em
    decisão informada — e evita descobrir só depois que o número escolhido
    marcou 200 pessoas.

    O recorde mundial continua sendo teto absoluto: nenhum ajuste aqui passa
    dele, porque limite físico não é questão de opinião.
    """
    from app import parametrosvel as pv

    escopo_cidade = current_user.cidade_ativa or ''

    if request.method == 'POST':
        modalidade = (request.form.get('modalidade') or '').strip()
        if modalidade not in dict(pv.MODALIDADES):
            flash('Modalidade desconhecida.', 'warning')
            return voltar('admin.parametros_velocidade')
        gravados = 0
        for chave, _mi, _ma, _rot in pv.FAIXAS:
            bruto = (request.form.get('teto_%s' % chave) or '').strip().replace(',', '.')
            try:
                valor = float(bruto) if bruto else 0.0
            except ValueError:
                valor = 0.0
            if pv.gravar('cidade', escopo_cidade, modalidade, chave, valor,
                         por=current_user.email):
                gravados += 1
        flash('Régua de %s salva. Ela vale para %s.'
              % (dict(pv.MODALIDADES).get(modalidade, modalidade),
                 escopo_cidade or 'todas as cidades'), 'success')
        return voltar('admin.parametros_velocidade')

    pv.ensure_schema()
    blocos = []
    for modalidade, rotulo in pv.MODALIDADES:
        atuais = pv.tetos('cidade', escopo_cidade, modalidade)
        efetivos, linhas = {}, []
        for chave, _mi, _ma, rot_faixa in pv.FAIXAS:
            valor = atuais.get(chave) or pv.SUGESTAO.get(modalidade, {}).get(chave)
            efetivos[chave] = valor
            linhas.append({
                'chave': chave,
                'rotulo': rot_faixa,
                'valor': atuais.get(chave) or '',
                'sugestao': pv.SUGESTAO.get(modalidade, {}).get(chave),
                'personalizado': bool(atuais.get(chave)),
            })
        previa = pv.quantas_ficam_de_fora(escopo_cidade, modalidade, efetivos)
        blocos.append({
            'modalidade': modalidade,
            'rotulo': rotulo,
            'linhas': linhas,
            'previa': previa,
            'pct': (round(100.0 * previa['fora'] / previa['total'], 1)
                    if previa['total'] else 0.0),
        })

    return render_template('admin/parametros_velocidade.html',
                           blocos=blocos, cidade=escopo_cidade)


@admin_bp.route('/textos-da-home', methods=['GET', 'POST'])
@login_required
@master_required
def textos_site():
    """Edita os textos da página inicial sem mexer em código.

    POR QUE ESTA TELA EXISTE (09/09/2026, pedido do dono)

    Trocar uma frase da porta de entrada exigia editar template em produção —
    e editar template em produção derruba o site por uma vírgula. Agora o dono
    ajusta o que fala com a cidade dele sozinho, e o pior que pode acontecer é
    o texto ficar feio.

    O que entra é limpo em `textossite.limpar()`: só negrito, itálico e
    sublinhado sobrevivem; o resto vira texto comum. Cor e tamanho não entram
    de propósito — quem escolhe isso é o site, senão a página vira colcha de
    retalhos.
    """
    from app import textossite

    escopo = ''      # a home é do projeto inteiro, não de uma cidade

    if request.method == 'POST':
        chave = (request.form.get('chave') or '').strip()
        acao = (request.form.get('acao') or 'salvar').strip()
        valido = {c for c, _r, _p in textossite.CATALOGO}
        if chave not in valido:
            flash('Não conheço esse trecho de texto.', 'warning')
            return voltar('admin.textos_site')

        if acao == 'restaurar':
            textossite.apagar(chave, cidade=escopo)
            flash('Trecho voltou ao texto original.', 'success')
        else:
            conteudo = request.form.get('conteudo') or ''
            if not conteudo.strip():
                # Vazio NAO e' erro: e' como se volta ao texto de fabrica.
                textossite.apagar(chave, cidade=escopo)
                flash('Trecho em branco — a página volta a usar o texto original.',
                      'info')
            elif textossite.gravar(chave, conteudo, por=current_user.email,
                                   cidade=escopo):
                flash('Texto salvo. Recarregue a página inicial para ver.', 'success')
            else:
                flash('Não consegui salvar agora. Tente de novo.', 'danger')
        return voltar('admin.textos_site')

    textossite.ensure_schema()
    itens = []
    for chave, rotulo, padrao in textossite.CATALOGO:
        linha = None
        try:
            from sqlalchemy import text as _sa_text
            linha = db.session.execute(_sa_text(
                'SELECT conteudo, atualizado_por, atualizado_em FROM textos_site '
                'WHERE chave = :k AND cidade = :c'),
                {'k': chave, 'c': escopo}).fetchone()
        except Exception as exc:
            logger.debug('textos: leitura de %r falhou (%s)', chave, exc)
        itens.append({
            'chave': chave,
            'rotulo': rotulo,
            'padrao': padrao,
            'valor': (linha[0] if linha else '') or '',
            'personalizado': bool(linha and (linha[0] or '').strip()),
            'atualizado_por': (linha[1] if linha else '') or '',
            'atualizado_em': (str(linha[2])[:16] if linha and linha[2] else ''),
        })
    return render_template('admin/textos_site.html', itens=itens)


def _garantir_containers_de_pe(escopo, minutos_minimo, por):
    """Garante que a janela de containers não vence antes de `minutos_minimo`
    a partir de agora. Nunca reduz um prazo maior já marcado, e nunca mexe em
    "até mandarem parar" (-1).

    POR QUE ISTO EXISTE (incidente de 09→10/09/2026, medido nos logs do site,
    do Guardião e do container WAHA, cruzados por horário):

    O Guardião (`Movimente-se_Guardiao.ps1`, a cada 10 minutos, fora da janela
    normal do ranking) só olha `automacao_estado.containers_ate` — ele não
    sabe se há uma publicação em andamento. Em 09/09 o dono marcou "manter de
    pé" por 30 min às 23:27:52 (prazo ~23:55:52) e clicou "Publicar agora" às
    23:45:24; a pipeline começou a processar às 23:50:11 e só terminou de
    coletar às 23:58:27 — mais devagar que o normal; a causa exata da demora
    não foi determinada (o log mostrou "Outro processamento está usando o
    Strava", que o código só emite quando há concorrência de verdade no
    `SharedPacer`, mas o candidato óbvio — um upload manual no mesmo horário —
    foi DESCARTADO: o `job` citado no log do site é o MESMO id do upload que
    a própria pipeline do ranking faz na Etapa 5, não um segundo processo).
    O que importa aqui, e está confirmado por prova direta, não suposição de
    horário: o Guardião viu o prazo vencido às 23:57:55 e mandou `docker stop`
    no WAHA/Postgres — a tentativa de envio às 23:58:48 bateu de frente com a
    porta já fechada: "Connection refused" na porta 3000
    (`send_ranking_20260909_235034.log`). A sessão do WhatsApp, cortada de
    forma abrupta em vez de terminar sozinha, ficou instável e caiu de novo na
    tentativa seguinte ("Sessão WhatsApp").
    A pipeline pode demorar mais que o normal por motivos ainda não
    totalmente mapeados — por isso a margem aqui é generosa (30 min, igual ao
    maior prazo que o próprio botão "manter de pé" oferece), não o mínimo que
    bastaria no caso feliz.

    Chamar isto ao registrar um pedido de publicação (`publicar_agora`)
    garante que a pipeline que ELA MESMA disparou tenha tempo de terminar
    antes do Guardião poder derrubar os containers.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import text as sa_text
    from app.blueprints.api import _ensure_estado_schema

    _ensure_estado_schema()
    linha = db.session.execute(sa_text(
        'SELECT containers_ate FROM automacao_estado WHERE cidade = :c'),
        {'c': escopo}).fetchone()
    atual = float(linha[0]) if linha and linha[0] is not None else 0.0
    if atual == -1:
        return  # "ate mandarem parar" ja cobre qualquer publicacao
    alvo = (datetime.now() + timedelta(minutes=minutos_minimo)).timestamp()
    if atual >= alvo:
        return  # ja ha prazo suficiente — nao mexe (nunca reduz)

    db.session.execute(sa_text(
        'INSERT INTO automacao_estado (cidade, atualizado_por, atualizado_em) '
        'VALUES (:c, :por, :em) ON CONFLICT(cidade) DO NOTHING'),
        {'c': escopo, 'por': por, 'em': datetime.now()})
    db.session.execute(sa_text(
        'UPDATE automacao_estado SET containers_ate = :ate, containers_por = :por '
        'WHERE cidade = :c'),
        {'ate': alvo, 'por': por, 'c': escopo})
    db.session.commit()


def _adiantar_execucao(escopo, por, motivo):
    """O clique nao espera a proxima passada da maquina (17/09/2026).

    POR QUE ISTO EXISTE
    Medido: "Manter de pe" clicado as 10:51:15 so subiu os containers as
    10:58:03 (passada do Guardiao, de 10 em 10 min); "Conectar" com o WhatsApp
    parado esperou ate 8 minutos pela fila (de 15 em 15). Quem clica esta
    olhando a tela e conclui que o sistema travou.

    Aqui o clique deixa um pedido no cofre; a consulta de minuto em minuto do
    Dell (`Movimente-se_PedidoImediato.ps1`) chama o Guardiao e a fila na hora.
    Nada muda no que o botao faz — isto e um adiantamento, nao um segundo
    caminho. Varios cliques viram UM pedido (o segundo ve o primeiro em aberto)
    e, no Dell, um roda por vez (trava de 17/09, item D5).

    Nunca levanta excecao: falhar aqui so devolve o comportamento antigo —
    esperar a passada.
    """
    try:
        from app import rodada
        rodada.pedir_execucao(escopo, por=por)
    except Exception as exc:
        logger.warning('Nao consegui adiantar a execucao (%s): %s', motivo, exc)


@admin_bp.route('/automacao/manter-de-pe', methods=['POST'])
@login_required
@master_required
def automacao_manter_de_pe():
    """Sobe os containers da aplicação e os mantém de pé por um prazo.

    NÃO CONFUNDIR COM "manter a máquina ligada" (09/09/2026, pedido do dono).
    São duas coisas diferentes e agora aparecem separadas na tela:

      - `manter_ligada`  -> mantém o COMPUTADOR aceso, sem prazo.
      - esta rota        -> mantém os CONTAINERS DA APLICAÇÃO de pé, com prazo.

    Outro serviço da máquina não entra nesta conta: ele fica de pé enquanto a máquina
    estiver ligada, porque alguém pode estar usando-o enquanto trabalha,
    e isso não tem hora marcada.

    Por que com prazo: sem ele, o operador sobe a automação para conferir uma
    coisa e ela fica de pé o resto do dia consumindo memória — foi o que
    aconteceu na madrugada de 09/09, quando deixei a âncora segurando tudo.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import text as sa_text
    from app.blueprints.api import _ensure_estado_schema

    escopo = current_user.cidade_ativa
    escolha = (request.form.get('duracao') or '').strip()

    # 'ate-desligar' vira -1: sem relógio, sai quando o dono mandar.
    minutos = {'30': 30, '60': 60, '120': 120, '180': 180}.get(escolha)
    if escolha == 'ate-desligar':
        ate = -1.0
    elif escolha == 'parar':
        ate = 0.0
    elif minutos:
        ate = (datetime.now() + timedelta(minutes=minutos)).timestamp()
    else:
        flash('Não entendi por quanto tempo manter a automação de pé.', 'warning')
        return voltar('admin.dashboard')

    _ensure_estado_schema()
    db.session.execute(sa_text(
        'INSERT INTO automacao_estado (cidade, atualizado_por, atualizado_em) '
        'VALUES (:c, :por, :em) ON CONFLICT(cidade) DO NOTHING'),
        {'c': escopo, 'por': current_user.email, 'em': datetime.now()})
    db.session.execute(sa_text(
        'UPDATE automacao_estado SET containers_ate = :ate, containers_por = :por '
        'WHERE cidade = :c'),
        {'ate': ate, 'por': current_user.email, 'c': escopo})
    db.session.commit()
    # Subir (ou parar) os containers e' trabalho do Guardiao: sem o atalho, ate
    # 10 minutos parado esperando a passada dele.
    _adiantar_execucao(escopo, current_user.email, 'manter de pe')

    # CLIQUE QUE NAO DEIXA RASTRO E CLIQUE PERDIDO (12/09/2026).
    #
    # Medido na madrugada de 12/09: o dono clicou em "Manter de pe" e o estado
    # mudou (containers_ativo=true, containers_por, containers_texto), mas o
    # log do site nao ganhou UMA linha. Na auditoria de 10/09 isso custou caro:
    # nao dava para separar "o Guardiao subiu sozinho" de "o dono pediu", e a
    # subida das 18:08 (que terminou em LOGOUT) ficou sem dono conhecido.
    if ate == 0:
        _quando_txt = 'parar na proxima passada'
    elif ate == -1:
        _quando_txt = 'ate mandarem parar'
    else:
        _quando_txt = 'ate as %s' % datetime.fromtimestamp(ate).strftime('%H:%M')
    logger.info('DONO: "Manter a automacao de pe" em %s por %s — %s.',
                escopo, current_user.email, _quando_txt)

    if ate == 0:
        flash('Pedido registrado: a automação faz a parada limpa na próxima '
              'passada (o outro serviço da máquina continua no ar).', 'success')
    elif ate == -1:
        flash('A automação vai ficar de pé até você mandar parar.', 'warning')
    else:
        quando = datetime.fromtimestamp(ate).strftime('%H:%M')
        flash('A automação fica de pé até as %s. Depois disso ela para sozinha, '
              'com parada limpa.' % quando, 'success')

    # Acordar a máquina é best-effort: falhar aqui não invalida o pedido, que
    # fica guardado e é atendido na próxima vez que ela falar com o site.
    if ate != 0:
        try:
            from app import ligarmaquina
            ligarmaquina.ligar(escopo)
        except Exception as exc:
            logger.info('manter-de-pe: não consegui acordar a máquina (%s)', exc)

    return voltar('admin.dashboard')


@admin_bp.route('/publicar-agora', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): tocar a cidade tambem e do admin
def publicar_agora():
    """PUBLICAR O RANKING AGORA — o botão do painel (15/08/2026).

    Não publica nada por conta própria: registra o pedido, acorda a máquina e
    deixa a PIPELINE fazer o que ela já faz — ligar, esperar tudo ficar
    saudável, coletar, processar e publicar, com as mesmas travas e os mesmos
    avisos. Um segundo caminho de publicação seria um segundo conjunto de
    regras para divergir do primeiro.
    """
    from app import rodada
    escopo = current_user.cidade_ativa
    ok, aviso = rodada.pedir(escopo, por=current_user.email,
                             forcar=(request.form.get('forcar') == 'sim'))
    if ok:
        # Garante que o Guardiao nao derruba os containers no meio da
        # publicacao que este clique acabou de disparar (incidente de
        # 09-10/09/2026, ver docstring de _garantir_containers_de_pe).
        try:
            _garantir_containers_de_pe(escopo, minutos_minimo=30,
                                        por=current_user.email)
        except Exception as exc:
            logger.warning('publicar_agora: nao consegui estender a janela '
                            'de containers (%s)', exc)
    flash(aviso, 'success' if ok else 'warning')
    return voltar('admin.dashboard')


@admin_bp.route('/publicar-agora/cancelar', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): tocar a cidade tambem e do admin
def publicar_agora_cancelar():
    from app import rodada
    rodada.cancelar(current_user.cidade_ativa, por=current_user.email)
    flash('Pedido cancelado — nada será publicado por causa dele.', 'info')
    return voltar('admin.dashboard')


@admin_bp.route('/executar-pedidos-agora', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): tocar a cidade tambem e do admin
def executar_pedidos_agora():
    """EXECUTAR PEDIDOS AGORA — o botão do painel (17/09/2026, pedido do dono).

    Faz andar na hora o que espera a máquina — containers pedidos no "Manter de
    pé", conexão de WhatsApp com o container parado, mensagens na fila — sem
    publicar ranking. Não executa nada daqui: registra o pedido; a consulta de
    minuto em minuto do Dell chama o Guardião e a fila em sequência. O clique
    fica no log de acesso e, por ser de gente logada, no canônico (`DONO`).
    """
    from app import rodada
    ok, aviso = rodada.pedir_execucao(current_user.cidade_ativa,
                                      por=current_user.email)
    flash(aviso, 'success' if ok else 'warning')
    return voltar('admin.dashboard')


@admin_bp.route('/qr-pareamento/<int:pedido_id>/<token>')
def qr_pareamento(pedido_id, token):
    """Serve o QR Code do pareamento. SEM login, de propósito (regra 17-A).

    Quem abre isto está no celular, no meio de um pareamento que já não deu
    certo pelo código — exigir senha ali é perder a pessoa. A proteção é o
    token assinado, que só vale para este pedido e morre com ele.

    O QR sai do WAHA na hora: ele muda a cada ~20 segundos, então guardar
    imagem não serviria de nada.
    """
    import hmac
    import requests
    from flask import Response
    from sqlalchemy import text as sa_text
    from app import whatsapp as wa

    if not hmac.compare_digest(token, wa.token_qr(pedido_id)):
        abort(404)

    linha = db.session.execute(sa_text(
        "SELECT cidade, sessao, situacao FROM sessoes_whatsapp WHERE id = :i"),
        {'i': pedido_id}).fetchone()
    if not linha:
        abort(404)
    cidade, sessao, situacao = linha[0], linha[1], linha[2]
    # Pedido encerrado não mostra QR: o link do e-mail antigo deixa de valer.
    if situacao not in ('pendente', 'em_andamento'):
        return ('<p style="font-family:sans-serif">Este pedido de conexão já '
                'terminou. Peça um código novo na tela do MoveMentes.</p>'), 410

    url, chave = _waha_config(cidade)
    if not url:
        abort(404)
    try:
        resp = requests.get('%s/api/%s/auth/qr' % (url.rstrip('/'), sessao),
                            headers={'X-Api-Key': chave or '',
                                     'Accept': 'image/png'},
                            timeout=20)
        if resp.status_code != 200:
            return ('<p style="font-family:sans-serif">O QR não está '
                    'disponível agora. Tente o código, ou peça outro na '
                    'tela.</p>'), 503
        return Response(resp.content,
                        mimetype=resp.headers.get('Content-Type', 'image/png'),
                        headers={'Cache-Control': 'no-store'})
    except Exception as exc:
        logger.warning('QR do pedido %s: %s', pedido_id, exc)
        return ('<p style="font-family:sans-serif">Não consegui falar com o '
                'WhatsApp agora.</p>'), 503


# O estado da máquina custa rede (até 8s de espera quando ela está fora). A tela
# do pareamento pergunta de 3 em 3 segundos: sem esta trava, cada admin com a
# página aberta viraria uma sonda contra o WAHA — e sonda demais foi o que
# derrubou o Chromium em 06/08.
_MAQUINA_CACHE = {}
_MAQUINA_CACHE_SEG = 30


def _sessao_prevista(usuario):
    """Como a sessão daquela pessoa vai se chamar. Nunca levanta exceção."""
    try:
        from app import publicador
        return publicador._sessao_do_usuario(usuario)
    except Exception as exc:
        logger.debug('Sem sessão prevista para %s: %s', usuario, exc)
        return '—'


_INSTANCIA_VELHA_MIN = 20   # item F do plano de 17/08: passou disso, o status para de ser afirmado como atual


def _instancias_com_idade(lista):
    """Acrescenta `visto_ha` (texto em português) e `visto_velho` a cada item.

    17/08/2026, item F do plano do MVP. `visto_em` já vinha do banco
    (`whatsapp.instancias`) e o template ignorava — por isso "FAILED"
    continuava na tela com o WhatsApp já reconectado: o status não tinha
    idade nenhuma, então nunca ficava velho aos olhos de quem olhava.
    A CONVENÇÃO É AVISO, NUNCA SUBSTITUIÇÃO: a idade aparece EMBAIXO do
    status, e só quando passa de ``_INSTANCIA_VELHA_MIN`` é que o próprio
    status (não só a legenda) para de ser afirmado como atual.
    """
    from app import whatsapp as wa
    agora = datetime.now()
    for item in lista:
        bruto = item.get('visto_em')
        quando = None
        if isinstance(bruto, datetime):
            quando = bruto
        elif bruto:
            try:
                quando = datetime.fromisoformat(str(bruto))
            except ValueError:
                quando = None
        if quando:
            segundos = int((agora - quando).total_seconds())
            item['visto_ha'] = wa.tempo_humano(segundos)
            item['visto_velho'] = segundos > (_INSTANCIA_VELHA_MIN * 60)
        else:
            item['visto_ha'] = None
            item['visto_velho'] = False
    return lista


def _estado_da_maquina(cidade):
    from app import maquina
    agora = time.time()
    guardado = _MAQUINA_CACHE.get(cidade)
    if guardado and (agora - guardado[0]) < _MAQUINA_CACHE_SEG:
        return guardado[1]
    try:
        estado = maquina.estado(cidade)
    except Exception as exc:
        logger.warning('Nao consegui apurar o estado da maquina: %s', exc)
        estado = None
    _MAQUINA_CACHE[cidade] = (agora, estado)
    return estado


@admin_bp.route('/whatsapp-sessao/andamento')
@login_required
@master_required
def whatsapp_andamento():
    """O que está acontecendo com cada pedido de pareamento, AO VIVO (P-75).

    A tela pergunta aqui de poucos em poucos segundos. Tudo sai do banco, então
    funciona com os dois workers do uWSGI — o pedido não mora na memória de um
    processo só.
    """
    from app import whatsapp as wa

    escopo = current_user.cidade_ativa
    linhas, algum_ativo = [], False
    for pessoa in wa.andamento_da_cidade(escopo):
        pedido = pessoa.get('pedido') or None
        if pedido:
            pedido = {
                'id': pedido['id'],
                'situacao': pedido.get('situacao'),
                'em_curso': pedido.get('em_curso', False),
                'etapa': pedido.get('etapa'),
                'etapa_titulo': pedido.get('etapa_titulo'),
                'codigo': pedido.get('codigo'),
                'sessao': pedido.get('sessao'),
                'instancia': pedido.get('instancia'),
                'porta': pedido.get('porta'),
                'mensagem': pedido.get('mensagem'),
                'ha_quanto_tempo': pedido.get('ha_quanto_tempo'),
                'passou_do_tempo': pedido.get('passou_do_tempo'),
                'limite_humano': pedido.get('limite_humano'),
                'passos': pedido.get('passos') or [],
                'historico': [{'hora': h['hora'], 'titulo': h['titulo'],
                               'mensagem': h['mensagem']}
                              for h in (pedido.get('historico') or [])],
                'motivo': pedido.get('motivo'),
            }
        algum_ativo = algum_ativo or pessoa.get('ativo', False)
        # O ESTADO VIAJA JUNTO (19/08/2026). Sem ele o JavaScript redesenhava o
        # card usando só o pedido e repunha, a cada 3 segundos, o "Conectado"
        # que o servidor tinha acabado de corrigir.
        est = pessoa.get('estado') or {}
        linhas.append({
            'usuario_id': pessoa['usuario_id'],
            'nome': pessoa['nome'],
            'papel': pessoa['papel'],
            'ordem': pessoa['ordem'],
            'ativo': pessoa.get('ativo', False),
            'pedido': pedido,
            'estado': {
                'chave': est.get('chave'),
                'rotulo': est.get('rotulo'),
                'detalhe': est.get('detalhe'),
                'cor': est.get('cor'),
                'pronta': est.get('pronta', False),
                # A camada de baixo viaja junto (16/09/2026): sem ela o
                # redesenho de 3 em 3 segundos apagava a linha "a automacao
                # nao esta rodando agora" que a pagina acabou de mostrar.
                'automacao_parada': est.get('automacao_parada', False),
            } if est else None,
        })

    estado = _estado_da_maquina(escopo) or {}
    return jsonify(
        equipe=linhas,
        algum_ativo=algum_ativo,
        maquina={'situacao': estado.get('situacao'),
                 'rotulo': estado.get('rotulo'),
                 'detalhe': estado.get('detalhe')},
    )


@admin_bp.route('/whatsapp-sessao', methods=['GET', 'POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): tocar a cidade tambem e do admin
def whatsapp_sessao():
    """Liga o WhatsApp de quem publica, sem precisar ir ate o computador.

    Existe porque a sessao cai de vez em quando (o proprio WhatsApp desvincula
    o aparelho) e, sem esta tela, era preciso ligar o notebook, abrir o painel
    do WAHA e ler o QR la. Aqui o codigo chega no celular de quem vai parear.

    A TELA TEM UMA COISA SO (P-75, 14/08/2026): a lista de quem publica, com o
    botao Conectar e o passo a passo ao vivo. Sairam com a reforma os campos da
    epoca em que a sessao era UMA POR CIDADE — nome da sessao, forma curta,
    "religar pelo celular" e "deixar na fila". Endereco, porta, chave e nome
    nao se digitam mais: quem conta e a maquina (regra 16-0-D).
    """
    from app import secretstore
    from app import whatsapp as wa

    escopo = current_user.cidade_ativa
    url, chave = _waha_config(escopo)

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()

        if acao == 'salvar':
            novo_url = (request.form.get('waha_url') or '').strip().rstrip('/')
            nova_chave = (request.form.get('waha_key') or '').strip()
            if not novo_url.startswith(('http://', 'https://')):
                flash('O endereco deve comecar com http:// ou https://.', 'danger')
                return voltar('admin.whatsapp_sessao')
            if not nova_chave and not chave:
                flash('Informe a chave de API do WAHA.', 'danger')
                return voltar('admin.whatsapp_sessao')
            ok = secretstore.set_value(secretstore.WAHA_URL, novo_url,
                                       scope=escopo, updated_by=current_user.email)
            # Campo da chave em branco = manter a que ja esta no cofre.
            if nova_chave:
                ok = secretstore.set_value(secretstore.WAHA_API_KEY, nova_chave,
                                           scope=escopo, updated_by=current_user.email) and ok
            responsavel = (request.form.get('responsavel') or '').strip()
            if responsavel:
                numero = normalizar_telefone_usuario(responsavel)
                if not numero:
                    flash('Telefone do responsável inválido — o resto foi salvo.', 'warning')
                else:
                    secretstore.set_value(secretstore.WAHA_NUMERO_MASTER, numero,
                                          scope=escopo, updated_by=current_user.email)
            flash('Configuracao salva.' if ok else 'Falha ao gravar no cofre.',
                  'success' if ok else 'danger')
            return voltar('admin.whatsapp_sessao')

        # SAIRAM DAQUI EM 14/08/2026, com a tela: `cidade_curta`, `nome_sessao`,
        # `reiniciar`, `recriar`, `pareamento` e `enfileirar`.
        #
        # Eram da época em que a sessão era UMA POR CIDADE. Depois que o nome
        # virou `<papel>-<cidade>` (regra 16-0-A) e que a máquina passou a
        # contar o que tem (`POST /api/instancias`), esses campos deixaram de
        # ter dono: `nome_sessao` gravava `palmeiropolis` — fora do padrão —, e
        # `pareamento`/`enfileirar` eram um segundo caminho para o que o botão
        # Conectar já faz, só que sem passo a passo e sem jeito de parar.
        #
        # Tudo passa pelo `provisionar` abaixo. Um caminho só, com tela.

        # ===== O BOTAO UNICO: conectar o MEU WhatsApp =====
        # O admin da cidade digita o telefone e mais nada. Porta, container,
        # banco, worker e nome de sessao sao decididos pelo sistema — ele nao
        # tem por que saber o que e nenhuma dessas coisas.
        if acao == 'provisionar':
            telefone = (request.form.get('telefone') or '').strip()
            alvo_id = request.form.get('usuario_id', type=int)

            alvo = current_user
            if alvo_id and alvo_id != current_user.id:
                candidato = Usuario.query.get(alvo_id)
                # Fronteira: so mexe em quem voce pode gerir, e da sua cidade.
                if (not candidato or candidato.cidade != escopo
                        or not current_user.pode_gerir(candidato)):
                    flash('Você não pode configurar o WhatsApp desse usuário.', 'danger')
                    return voltar('admin.whatsapp_sessao')
                alvo = candidato

            if not (alvo.ordem_envio or 0):
                flash('Antes defina a ordem de envio deste usuário (principal ou '
                      'suplente) em "Quem publica no grupo".', 'warning')
                return redirect(url_for('admin.postagem'))

            # QUEM LIGA A MAQUINA E O ATENDIMENTO, NAO ESTA ROTA (16/08/2026).
            #
            # Aqui havia UM disparo de Wake-on-LAN, com um comentario dizendo
            # que "de desligado total nada aqui o liga" — o que era verdade
            # enquanto o recurso estava desarmado na placa, e deixou de ser: o
            # WoL LIGA a maquina desligada, e e o Agendador que trata a
            # suspensao (regra 21-H). Um pacote so, disparado no clique, tambem
            # nao bastava: pacote magico nao tem resposta, e a maquina pode
            # estar no meio de um boot. Agora quem manda o sinal e repete e o
            # `whatsapp.atender_no_site`, que fica insistindo e ESPERANDO ela
            # responder — e escreve cada tentativa na tela.
            #
            # Fica so o aviso do caso em que nem da para tentar.
            aviso_maquina = ''
            try:
                from app import ligarmaquina
                if not ligarmaquina.configurado(escopo):
                    aviso_maquina = (' (não há Wake-on-LAN cadastrado nesta cidade: '
                                     'se a máquina estiver desligada, ligue-a na mão '
                                     'para o pedido ser atendido.)')
            except Exception as exc:
                logger.warning('Nao consegui conferir o Wake-on-LAN: %s', exc)

            ok, aviso = wa.pedir_vinculacao(
                escopo, telefone or alvo.telefone,
                pedido_por=current_user.email,
                recriar=(request.form.get('recriar') == 'sim'),
                usuario=alvo, email=alvo.email)
            if ok:
                # Com o WhatsApp de pe o site ja fala direto com ele; com o
                # container parado, quem cria a sessao e o `provisionar.py`,
                # que so roda quando a fila passa (ate 15 min).
                _adiantar_execucao(escopo, current_user.email, 'conectar')
            flash(aviso + aviso_maquina, 'success' if ok else 'danger')

            # RESOLVER NA HORA QUANDO DER (14/08/2026).
            #
            # Quem atende a fila de pareamento é o `provisionar.py`, dentro do
            # `executar.sh` — que roda no boot, às 10:05, às 21:35 e nos
            # horários do ranking. Com a máquina JÁ ligada e a fila de mensagens
            # vazia, o clique esperava horas por uma rodada. Quando a instância
            # daquele papel já existe, não há nada para o Docker fazer: criar a
            # sessão e pedir o código é chamada HTTP, e o site sabe fazer.
            #
            # Vai em thread porque a máquina pode estar DESLIGADA: o
            # atendimento manda o sinal de ligar, espera ela responder (até 20
            # min, repetindo o pacote) e só então fala com o WAHA — quem conta o
            # andamento é a tela ao vivo (P-75). Falhando, o pedido continua
            # pendente para a máquina: atalho, nunca dono (regra 16-P).
            if ok:
                pedido_id = wa.pedido_aberto_do_usuario(escopo, alvo.id)
                if pedido_id:
                    threading.Thread(
                        target=wa.atender_no_site,
                        args=(current_app._get_current_object(), escopo,
                              pedido_id),
                        daemon=True).start()
            return voltar('admin.whatsapp_sessao')

        if acao == 'trocar_versao_waha':
            # TROCA DE VERSAO DO WAHA (19/09/2026, item 67) — so o master
            # supremo. A maquina executa (`scripts/versoes.py`).
            if not current_user.is_supremo:
                flash('Só o master supremo troca a versão do WhatsApp.', 'danger')
                return voltar('admin.whatsapp_sessao')
            if not request.form.get('ciente'):
                flash('Marque a ciência para pedir a troca de versão.', 'warning')
                return voltar('admin.whatsapp_sessao')
            ok, recado = wa.pedir_troca_de_versao(escopo, request.form.get('tag'),
                                                  por=current_user.email)
            if ok:
                try:
                    _garantir_containers_de_pe(escopo, minutos_minimo=30,
                                               por=current_user.email)
                except Exception as exc:
                    logger.warning('versao: nao consegui segurar os containers (%s)', exc)
                _adiantar_execucao(escopo, current_user.email, 'trocar versao do WAHA')
            flash(recado, 'success' if ok else 'warning')
            return voltar('admin.whatsapp_sessao')

        if acao == 'faxina_cidade':
            # FAXINA DA CIDADE (18/09/2026, plano A10 + item 5). Só alcança o
            # container e os dados da cidade ativa de quem clica (regra 3).
            if not current_user.pode_administrar:
                flash('Você não pode pedir faxina.', 'danger')
                return voltar('admin.whatsapp_sessao')
            if not request.form.get('confirmo'):
                flash('Marque a confirmação para pedir a faxina.', 'warning')
                return voltar('admin.whatsapp_sessao')
            ok, recado = wa.pedir_faxina(escopo, por=current_user.email)
            if ok:
                # A faxina precisa do WhatsApp de pé para perguntar às sessões.
                try:
                    _garantir_containers_de_pe(escopo, minutos_minimo=30,
                                               por=current_user.email)
                except Exception as exc:
                    logger.warning('faxina: nao consegui segurar os containers (%s)', exc)
                _adiantar_execucao(escopo, current_user.email, 'faxina da cidade')
            flash(recado, 'success' if ok else 'warning')
            return voltar('admin.whatsapp_sessao')

        if acao == 'apagar_sessao':
            # APAGAR SESSÃO — O BOTÃO QUE FALTAVA (08/09/2026).
            #
            # Sem ele, o dono tirava a sessão do site e ela continuava viva no
            # WAHA: memória gasta, e uma sessão que o site não enxerga mais.
            # A regra de quem pode sair mora em `whatsapp.apagar_sessao` —
            # aqui só se confere QUEM está pedindo.
            if not current_user.pode_administrar:
                flash('Você não pode apagar sessões.', 'danger')
                return voltar('admin.whatsapp_sessao')
            nome_alvo = (request.form.get('sessao') or '').strip()
            # A TRAVA CONTINUA; O QUE MUDA É O QUE SE PEDE (13/09/2026).
            #
            # Antes era preciso DIGITAR o nome da sessão (`principal-pls`).
            # Quem clica não tem como saber esse nome: ele vive dentro do
            # container, e em outra cidade será outro. O dono travou no próprio
            # botão por não saber o que escrever — trava que impede o dono de
            # usar o sistema não protege nada, só transfere o problema.
            #
            # Fica uma confirmação que quem clica CONSEGUE dar, e que continua
            # exigindo um ato deliberado: marcar que está ciente de que este
            # número sai do ar e vai precisar de novo pareamento. Clique único
            # continua não bastando.
            if not request.form.get('confirmo'):
                flash('Marque a confirmação para apagar a sessão.', 'warning')
                return voltar('admin.whatsapp_sessao')
            ok, recado = wa.apagar_sessao(
                escopo, nome_alvo, por=current_user.email,
                confirmado_working=bool(request.form.get('ciente_working')))
            if not ok:
                # Falhou porque o WhatsApp nao respondeu? O atalho faz a maquina
                # passar agora; com o container de pe, o proximo clique limpa.
                _adiantar_execucao(escopo, current_user.email, 'limpar a sessao')
            flash(recado, 'success' if ok else 'danger')
            return voltar('admin.whatsapp_sessao')

        if acao == 'definir_papel':
            # QUEM É O SUPLENTE SE DECIDE AQUI TAMBÉM (pedido do dono, 14/08).
            # É o MESMO campo (`ordem_envio`) que a tela "Quem publica no grupo"
            # grava — não um segundo lugar com a mesma informação. Mudou aqui,
            # muda o nome da sessão (`publicador._sessao_do_usuario`), quem
            # publica e para quem vai o alerta cruzado.
            alvo_id = request.form.get('usuario_id', type=int)
            ordem = request.form.get('ordem', type=int)
            alvo = Usuario.query.get(alvo_id) if alvo_id else None
            if (not alvo or alvo.cidade != escopo
                    or not current_user.pode_gerir(alvo)):
                flash('Você não pode mudar o papel desse usuário.', 'danger')
                return voltar('admin.whatsapp_sessao')
            if ordem not in (0, 1, 2):
                flash('Papel desconhecido.', 'warning')
                return voltar('admin.whatsapp_sessao')

            # DOIS PRINCIPAIS É PIOR QUE NENHUM: quem estava na vaga sai dela.
            # Sem isto, `publicador.conflitos` acusaria "duas pessoas na mesma
            # posição" e o sistema publicaria por uma sem dizer qual.
            trocado = None
            if ordem:
                anterior = Usuario.query.filter(
                    Usuario.cidade == escopo, Usuario.id != alvo.id,
                    Usuario.ordem_envio == ordem).first()
                if anterior:
                    anterior.ordem_envio = 0
                    trocado = anterior.nome
            alvo.ordem_envio = ordem
            db.session.commit()
            nome_papel = {0: 'não publica', 1: 'principal', 2: 'suplente'}[ordem]
            flash('%s agora é %s.%s A sessão dele passa a se chamar "%s".'
                  % (alvo.nome, nome_papel,
                     (' %s saiu dessa posição.' % trocado) if trocado else '',
                     _sessao_prevista(alvo)), 'success')
            logger.warning('Papel de %s (%s) mudou para %s por %s',
                           alvo.nome, escopo, nome_papel, current_user.email)
            return voltar('admin.whatsapp_sessao')

        if acao == 'parar_pedido':
            # O BOTAO DE PARAR, por pessoa (12/08). Sem ele, um pedido travado
            # em "Ligando o computador…" nao tinha como ser interrompido pela
            # tela — so mexendo no banco.
            alvo_id = request.form.get('usuario_id', type=int)
            if not alvo_id:
                flash('Não sei qual pedido parar.', 'warning')
                return voltar('admin.whatsapp_sessao')
            ok, aviso = wa.cancelar_pedido_de(alvo_id, escopo,
                                              por=current_user.email)
            flash(aviso, 'success' if ok else 'warning')
            return voltar('admin.whatsapp_sessao')

        if acao == 'ligar_maquina':
            from app import ligarmaquina
            ok, mensagem = ligarmaquina.ligar(escopo)
            flash(mensagem, 'info' if ok else 'warning')
            logger.info('Pedido de ligar a maquina por %s (cidade=%s): %s',
                        current_user.email, escopo, ok)
            return voltar('admin.whatsapp_sessao')

        if acao == 'desligar_maquina':
            # 17/08/2026, item G do plano do MVP. So grava o pedido — quem
            # entrega e a propria maquina, na proxima vez que perguntar
            # `GET /api/fila/pendentes` (ate 15 min). Nao existe "desligar
            # agora, na hora": ela para os programas na ordem certa primeiro.
            secretstore.set_value(secretstore.PEDIDO_DESLIGAR_MAQUINA,
                                  datetime.now().isoformat(timespec='seconds'),
                                  scope=escopo, updated_by=current_user.email)
            # Quem desliga e a tarefa da fila, de 15 em 15 minutos. Com o
            # atalho, a parada limpa comeca no proximo minuto.
            _adiantar_execucao(escopo, current_user.email, 'desligar a maquina')

            # DESLIGAR VENCE AS TRAVAS (09/09/2026, decisão do dono).
            #
            # Havia duas alavancas que impedem a máquina de parar:
            #   - `manter_ligada`   -> mantém o COMPUTADOR aceso (sem prazo)
            #   - `containers_ate`  -> mantém a AUTOMAÇÃO de pé (com prazo)
            #
            # Quem clica em "desligar" está mandando parar, e teria de lembrar
            # de desarmar as duas antes — ou o pedido ficaria preso, sem
            # ninguém entender por quê. Pior: as duas ficam em telas
            # diferentes. Então desligar desarma as duas aqui, de propósito, e
            # diz que fez.
            #
            # Isso NÃO atropela a parada limpa: o pedido continua sendo
            # entregue pela própria máquina, que para os programas na ordem e
            # confere a cópia da sessão antes (regra 17). Desarmar a trava só
            # tira o impedimento; não desliga nada na marra.
            desarmadas = []
            try:
                if (secretstore.get(secretstore.MANTER_LIGADA, scope=escopo)
                        or '').strip().lower() == 'sim':
                    secretstore.set_value(secretstore.MANTER_LIGADA, 'nao',
                                          scope=escopo,
                                          updated_by=current_user.email)
                    desarmadas.append('"manter a máquina ligada"')
            except Exception as exc:
                logger.warning('desligar: não consegui desarmar manter_ligada (%s)', exc)

            try:
                from sqlalchemy import text as _sa_text
                from app.blueprints.api import _ensure_estado_schema, _janela_containers
                _ensure_estado_schema()
                if (_janela_containers(escopo) or {}).get('containers_ativo'):
                    db.session.execute(_sa_text(
                        'UPDATE automacao_estado SET containers_ate = 0, '
                        'containers_por = :por WHERE cidade = :c'),
                        {'por': current_user.email, 'c': escopo})
                    db.session.commit()
                    desarmadas.append('"automação de pé"')
            except Exception as exc:
                db.session.rollback()
                logger.warning('desligar: não consegui desarmar a janela dos '
                               'containers (%s)', exc)

            if desarmadas:
                flash('Também desarmei %s — desligar tem prioridade sobre as '
                      'travas do painel.' % ' e '.join(desarmadas), 'info')

            logger.info('Pedido de DESLIGAR a maquina por %s (cidade=%s); '
                        'travas desarmadas: %s',
                        current_user.email, escopo, desarmadas or 'nenhuma')
            flash('Pedido registrado. Ela para os programas na ordem certa e '
                  'desliga — leva uns 1-2 minutos, dentro de até 15. Se tiver '
                  'pareamento em andamento ou mensagem esperando na fila, ela '
                  'termina isso primeiro. Para ligar de novo, use o botão Ligar.',
                  'warning')
            return voltar('admin.whatsapp_sessao')

        if acao == 'cancelar_desligar_maquina':
            secretstore.delete(secretstore.PEDIDO_DESLIGAR_MAQUINA, scope=escopo)
            logger.info('Pedido de desligar a maquina CANCELADO por %s (cidade=%s)',
                        current_user.email, escopo)
            flash('Pedido de desligar cancelado.', 'success')
            return voltar('admin.whatsapp_sessao')

        if acao == 'salvar_maquina':
            from app import ligarmaquina
            # Uma OU MAIS placas, separadas por virgula (08/09/2026): a
            # maquina pode acordar pela cabeada (com cabo) ou pela Wi-Fi (sem
            # cabo, suspensa) -- ver `ligarmaquina.normalizar_macs`.
            macs = ligarmaquina.normalizar_macs(request.form.get('wol_mac'))
            difusao = (request.form.get('wol_broadcast') or '').strip()
            if (request.form.get('wol_mac') or '').strip() and not macs:
                flash('Endereço da placa de rede inválido. São 12 caracteres '
                      'por placa, como 00:1A:2B:3C:4D:5E -- mais de uma placa, '
                      'separe por vírgula.', 'danger')
                return voltar('admin.whatsapp_sessao')
            if macs:
                secretstore.set_value(secretstore.WOL_MAC, ', '.join(macs),
                                      scope=escopo, updated_by=current_user.email)
            else:
                secretstore.delete(secretstore.WOL_MAC, scope=escopo)
            if difusao:
                secretstore.set_value(secretstore.WOL_BROADCAST, difusao,
                                      scope=escopo, updated_by=current_user.email)
            flash('Endereço da máquina salvo.', 'success')
            return voltar('admin.whatsapp_sessao')

        flash('Acao desconhecida.', 'warning')
        return voltar('admin.whatsapp_sessao')

    # ----- GET -----
    #
    # NAO SE PERGUNTA MAIS A SESSAO AQUI (14/08/2026). Esta tela fazia duas
    # chamadas HTTP ao WAHA a cada carregamento — `/api/sessions/<nome>` e
    # `/api/sessions` — para montar um bloco "Situacao agora" e uma lista de
    # "outras sessoes" que eram da epoca de UMA sessao por cidade. Quem responde
    # isso agora e o relato da maquina (`instancias`), que ja vem do banco e
    # cobre as duas instancias, nao uma. Numa tela que se atualiza sozinha,
    # chamada de rede a toa e latencia que aparece.
    erro = None

    # A MAQUINA DA AUTOMACAO — os tres estados da regra 21-G. Mora nesta tela
    # porque e a tela que o admin abre quando "o WhatsApp nao publicou": antes
    # de mexer em sessao, a primeira pergunta e se a maquina esta de pe.
    #
    # SEM ESTA EXIBICAO, `sem_endereco` ERA INVISIVEL. O vigia so reclama de
    # `quebrada` (de proposito: desligada e o normal), entao uma cidade sem
    # endereco cadastrado parecia identica a uma cidade saudavel. Foi
    # exatamente o que aconteceu ate 06/08 — o conserto do P-44 estava pronto e
    # nunca tinha rodado uma vez sequer, porque o cofre nao tinha endereco
    # nenhum. Protecao que ninguem ve nao protege.
    maquina_estado = _estado_da_maquina(escopo)
    # TOPO POR CAMADA, AO VIVO (18/09/2026, plano A3 / Pendente 2).
    try:
        from app import maquina as _maq
        camadas = _maq.camadas(escopo)
    except Exception as exc:
        logger.warning('Nao consegui apurar as camadas da maquina: %s', exc)
        camadas = None

    # O PEDIDO DE DESLIGAR, PARA A TELA MOSTRAR (item G). Mesma janela de 30
    # min que `api.fila_pendentes` usa para decidir se ainda vale.
    pedido_desligar = {'ativo': False, 'ha': None}
    try:
        _pedido_em = secretstore.get(secretstore.PEDIDO_DESLIGAR_MAQUINA, scope=escopo)
        if _pedido_em:
            _quando = datetime.fromisoformat(str(_pedido_em))
            _segundos = int((datetime.now() - _quando).total_seconds())
            if _segundos <= 30 * 60:
                pedido_desligar = {'ativo': True, 'ha': wa.tempo_humano(_segundos)}
            else:
                secretstore.delete(secretstore.PEDIDO_DESLIGAR_MAQUINA, scope=escopo)
    except Exception as exc:
        logger.warning('Nao consegui ver o pedido de desligar: %s', exc)

    return render_template(
        'admin/whatsapp_sessao.html',
        waha_url=url, waha_key_ok=bool(chave), erro=erro,
        maquina_estado=maquina_estado,
        camadas=camadas,
        faxina_pedida=wa.faxina_pedida(escopo),
        faxina_ultima=wa.ultima_faxina(escopo),
        versoes=wa.versoes(escopo) if current_user.is_supremo else None,
        versao_pedida=wa.troca_de_versao_pedida(escopo) if current_user.is_supremo else None,
        versao_ultima=wa.ultima_troca_de_versao(escopo) if current_user.is_supremo else None,
        escopo=escopo,
        # O Wake-on-LAN morava numa ação sem tela nenhuma: dava para gravar o
        # endereço da placa por POST e não havia campo para isso em lugar
        # nenhum do site. Agora ele aparece no bloco recolhido do fim.
        wol_mac=secretstore.get(secretstore.WOL_MAC, scope=escopo) or '',
        wol_broadcast=secretstore.get(secretstore.WOL_BROADCAST, scope=escopo) or '',
        responsavel=wa.numero_responsavel(escopo),
        equipe=wa.andamento_da_cidade(escopo),
        # O QUE A MÁQUINA RELATOU — a tela deixa de perguntar endereço, porta e
        # nome de sessão porque agora ela SABE (P-64).
        instancias=_instancias_com_idade(wa.instancias(escopo)),
        pedido_desligar=pedido_desligar,
        painel_cidades=(wa.situacao_por_cidade() if current_user.is_supremo else None),
    )


@admin_bp.route('/automacao', methods=['GET', 'POST'])
@login_required
@supremo_required  # 19/09/2026 (item 73): cookie/token/endereco e do supremo
def automacao():
    """Token da API de automacao (WAHA no computador da automacao).

    O token e gerado aqui, fica no cofre em instance/ (fora do banco, fora do
    git e do backup normal) e identifica a cidade do master que o gerou. A
    tela exibe o valor UMA unica vez, logo apos gerar; depois disso apenas
    metadados (impressao digital, data, autor).
    """
    from app import secretstore

    escopo = current_user.cidade_ativa
    token_novo = None

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()

        if acao == 'gerar':
            candidato = 'mov_' + secrets.token_urlsafe(32)
            if not secretstore.set_value(secretstore.AUTOMATION_TOKEN, candidato,
                                         scope=escopo, updated_by=current_user.email):
                flash('Nao foi possivel gravar o token. Verifique as permissoes da pasta instance.', 'danger')
                return voltar('admin.automacao')
            token_novo = candidato
            logger.info('Token de automacao gerado por %s (cidade=%s)', current_user.email, escopo)
            flash('Token gerado. Copie agora: ele nao sera exibido novamente.', 'success')
            # Sem redirect de proposito: o valor aparece uma unica vez nesta
            # resposta e nao viaja em sessao/flash.

        elif acao == 'remover':
            secretstore.delete(secretstore.AUTOMATION_TOKEN, scope=escopo)
            logger.info('Token de automacao removido por %s (cidade=%s)', current_user.email, escopo)
            flash('Token removido. A API de automacao desta cidade foi desativada.', 'info')
            return voltar('admin.automacao')

        elif acao == 'gerar_orquestrador' and current_user.is_supremo:
            # Token da MAQUINA, nao de uma cidade: so alcanca a fila de
            # vinculacao do WhatsApp. Sem ele, uma cidade que ainda nao tem
            # token proprio nunca conseguiria vincular o primeiro aparelho.
            candidato = 'orq_' + secrets.token_urlsafe(32)
            if not secretstore.set_value(secretstore.AUTOMATION_TOKEN_GLOBAL, candidato,
                                         updated_by=current_user.email):
                flash('Nao foi possivel gravar o token do orquestrador.', 'danger')
                return voltar('admin.automacao')
            token_novo = candidato
            logger.warning('Token do orquestrador gerado por %s', current_user.email)
            flash('Token do orquestrador gerado. Copie agora: ele não será exibido novamente.', 'success')

        elif acao == 'remover_orquestrador' and current_user.is_supremo:
            secretstore.delete(secretstore.AUTOMATION_TOKEN_GLOBAL)
            flash('Token do orquestrador removido.', 'info')
            return voltar('admin.automacao')

        elif acao:
            return voltar('admin.automacao')

    dados = secretstore.info(secretstore.AUTOMATION_TOKEN, scope=escopo)
    if dados.get('atualizado_em'):
        dados['atualizado_em_texto'] = datetime.fromtimestamp(
            dados['atualizado_em']
        ).strftime('%d/%m/%Y %H:%M')
    else:
        dados['atualizado_em_texto'] = ''
    orquestrador = secretstore.info(secretstore.AUTOMATION_TOKEN_GLOBAL)
    if orquestrador.get('atualizado_em'):
        orquestrador['atualizado_em_texto'] = datetime.fromtimestamp(
            orquestrador['atualizado_em']
        ).strftime('%d/%m/%Y %H:%M')
    else:
        orquestrador['atualizado_em_texto'] = ''
    return render_template(
        'admin/automacao.html',
        info=dados,
        escopo=escopo,
        token_novo=token_novo,
        orquestrador=orquestrador,
        base_url=request.url_root.rstrip('/'),
    )


def _escrever_andamento(job_id, cidade, **kw):
    """Escreve o andamento no MESMO arquivo que a fila da tela de upload le.

    Ate 07/09/2026 este trabalho tinha um registro so' dele
    (`_MAINTENANCE_JOBS`), que vivia na memoria do worker e nao aparecia em
    lugar nenhum. Agora ele e um trabalho como os outros: mesma pasta, mesmo
    estagio, mesma linha na tela -- e por isso mesmo, mesma vaga.

    `ok` e `done` nao passam adiante de proposito: na fila quem diz que
    acabou e' o ESTAGIO ('done'/'error'), e ter duas fontes para a mesma
    resposta e' ter uma delas errada mais cedo ou mais tarde.
    """
    from app.blueprints.upload import _set_progress
    _set_progress(job_id, kw.get('stage') or 'running',
                  kw.get('message') or '', int(kw.get('pct') or 0),
                  'atualizar_perfis', cidade)


def _run_atualizar_fotos_job(app, job_id, cidade, o_que='foto', sids=None,
                             automatico=False):
    """Busca no Strava (sessao ativa) a foto OU o nome dos atletas da cidade.

    `o_que` e' 'foto' ou 'nome' -- sao dois botoes separados desde 06/09/2026.
    Antes era um so, e ai um obrigava o outro: para corrigir um nome era
    preciso gastar requisicao com foto, e vice-versa.

    Roda em segundo plano com o mesmo controle de taxa do scraping. No modo
    'foto', atleta com foto de menos de 30 dias e' pulado. Bloqueio do Strava
    encerra com parciais (repetir mais tarde continua de onde parou).
    """
    def _andamento(jid, **kw):
        # A cidade vem do trabalho, nao de cada chamada -- sao oito, e repetir
        # a cidade em todas seria oito chances de esquecer numa delas.
        _escrever_andamento(jid, cidade, **kw)

    with app.app_context():
        from app import secretstore
        from app.blueprints.processing import (
            build_strava_throttle, fetch_athlete_avatar_url, StravaRateLimited,
            StravaLimiteDeVolume, fetch_athlete_nome,
        )
        try:
            cookie = secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)
            if not cookie:
                _andamento(
                    job_id, stage='error', done=True, ok=False, pct=0,
                    message='Sessao do Strava nao configurada (Painel > Sessao do Strava).')
                return
            # A RETOMADA MANDA DE ONDE PARAR (P-128, 07/09/2026): quando
            # `sids` vem preenchido, e' a fila que sobrou da passada
            # anterior -- nao se remonta a lista, senao a retomada
            # recomecaria do primeiro atleta a cada vez.
            if sids is None:
                sids = [str(r[0]) for r in db.session.query(Activity.id_usuario_strava)
                        .filter(Activity.cidade == cidade,
                                Activity.id_usuario_strava != '0')
                        .distinct().all()]
            else:
                sids = [str(s) for s in sids]
            atletas = {a.strava_id: a for a in
                       Atleta.query.filter(Atleta.strava_id.in_(sids)).all()}
            agora = datetime.utcnow()
            pendentes = []
            for sid in sids:
                atleta = atletas.get(sid)
                # A JANELA DE 30 DIAS E SO DA FOTO: nome muda quando a
                # pessoa quer, e ela pode ter mudado ontem.
                if (o_que == 'foto' and atleta and atleta.foto_url
                        and atleta.foto_atualizada_em
                        and (agora - atleta.foto_atualizada_em).days < 30):
                    continue
                pendentes.append(sid)
            if not pendentes:
                # CARIMBA TAMBEM QUANDO NAO HA NADA A FAZER (15/09/2026).
                # Este ramo saia sem carimbar, e sem carimbo `vencida()` dizia
                # "sim" para sempre: o vigia reenfileirava a passada a cada 15
                # minutos, dia e noite. Medido no log de 15/09 — Minaçu (cidade
                # sem atividade, portanto sem nada a atualizar) abriu um
                # processo novo 4 vezes por hora, das 00h as 23h.
                try:
                    from app import perfilauto
                    perfilauto.marcar_concluida(cidade, o_que)
                except Exception as _e:
                    logger.debug('Nao consegui marcar a passada: %s', _e)
                _andamento(job_id, stage='done', done=True, ok=True, pct=100,
                                     message='Todas as fotos ja estao atualizadas.')
                return
            throttle = build_strava_throttle(mode='mes_atual', cidade=cidade)
            atualizadas = 0
            nomes = 0
            sem_foto = 0
            # A TRAVA DO NOME REPETIDO (29/08/2026, depois do estrago).
            # Duas pessoas podem ter o mesmo nome; TRES seguidas, lidas da
            # mesma fonte, nao e' coincidencia -- e' a leitura pegando
            # sempre o mesmo lugar errado. Foi o que aconteceu: o nome da
            # conta do cookie foi gravado em 62 atletas antes de alguem
            # perceber, e nome escrito em cima de nome nao volta sozinho.
            vistos_por_nome = {}
            nome_suspeito = None
            for pos, sid in enumerate(pendentes, start=1):
                _andamento(
                    job_id, stage='running',
                    message=('Buscando %s %d/%d no Strava...'
                             % ('a foto' if o_que == 'foto' else 'o nome',
                                pos, len(pendentes))),
                    pct=min(95, int(95 * pos / len(pendentes))))
                try:
                    # UMA COISA POR VEZ (06/09/2026). Ate aqui a foto e o
                    # nome vinham da mesma resposta, o que parecia economia
                    # -- e era, ate o nome vir errado e levar a foto junto
                    # no mesmo commit. Sao duas leituras com ancoras
                    # diferentes e riscos diferentes; agora sao dois botoes.
                    foto = nome_strava = None
                    if o_que == 'foto':
                        foto = fetch_athlete_avatar_url(
                            sid, cookie, throttle=throttle)
                    else:
                        nome_strava = fetch_athlete_nome(
                            sid, cookie, throttle=throttle)
                except StravaLimiteDeVolume as limite:
                    # GUARDA DE ONDE PARAR (P-128). Antes o trabalho
                    # terminava aqui e o clique seguinte comecava tudo de
                    # novo -- parando no mesmo lugar. Agora a fila que
                    # sobrou fica em disco e o vigia continua sozinho
                    # quando a janela virar.
                    try:
                        from app import perfilauto
                        perfilauto.guardar_pendentes(
                            cidade, o_que, pendentes[pos - 1:],
                            'teto de %s' % limite.janela)
                    except Exception as _e:
                        logger.debug('Nao consegui guardar a fila: %s', _e)
                    # O TETO E NOSSO, NAO E ERRO (29/08/2026). Ate aqui esta
                    # excecao caia no `except Exception` la embaixo e o job
                    # morria com "Erro: ...", sem dizer que bastava esperar.
                    # Foi assim que ele travou em 55/77 na frente do dono.
                    _andamento(
                        job_id, stage='done', done=True, ok=True, pct=100,
                        message=('Parei em %d de %d: bateu o limite de leituras '
                                 'por %s. %d foto(s) e %d nome(s) já foram '
                                 'atualizados. O resto entra na fila sozinho em '
                                 'cerca de %d minuto(s) — não precisa clicar.'
                                 % (pos, len(pendentes), limite.janela,
                                    atualizadas, nomes,
                                    max(1, int(limite.segundos // 60)))))
                    return
                except StravaRateLimited:
                    # GUARDA O QUE FALTOU, IGUAL AO TETO ACIMA (07/09/2026).
                    # Este ramo nao guardava: quem sobrasse so' voltaria a ser
                    # lido quando o prazo da passada vencesse (7 ou 30 dias) --
                    # e a mensagem na tela prometia a volta sozinho. Prometer
                    # sem guardar e' pior que nao prometer.
                    try:
                        from app import perfilauto
                        perfilauto.guardar_pendentes(
                            cidade, o_que, pendentes[pos - 1:],
                            'pausa pedida pelo Strava')
                    except Exception as _e:
                        logger.debug('Nao consegui guardar a fila: %s', _e)
                    _andamento(
                        job_id, stage='done', done=True, ok=True, pct=100,
                        message=(f'O Strava pediu pausa: {atualizadas} foto(s) e '
                                 f'{nomes} nome(s) atualizados. Os demais voltam '
                                 'para a fila sozinhos quando a pausa acabar.'))
                    return
                if not foto and not nome_strava:
                    sem_foto += 1
                    continue
                atleta = atletas.get(sid)
                if not atleta:
                    atleta = Atleta.query.filter_by(strava_id=sid).first()
                if not atleta:
                    atleta = Atleta(strava_id=sid)
                    db.session.add(atleta)
                atletas[sid] = atleta
                if foto:
                    atleta.foto_url = foto
                    atleta.foto_atualizada_em = datetime.utcnow()
                    atualizadas += 1
                # NOME SO SE GRAVA COM CERTEZA DE QUEM E (06/09/2026).
                # `fetch_athlete_nome` ja tem duas travas proprias (a URL
                # final tem de ser a do perfil pedido; nome igual ao da
                # conta do cookie e recusado). Esta terceira fica aqui de
                # proposito, no GRAVADOR: as de la sabem o que procurar,
                # esta aqui pega o que ninguem previu.
                if nome_strava:
                    vistos_por_nome[nome_strava] = \
                        vistos_por_nome.get(nome_strava, 0) + 1
                    if vistos_por_nome[nome_strava] >= _MAX_MESMO_NOME:
                        # PARA TUDO E DESFAZ: o que ja foi gravado nesta
                        # passada com este nome tambem esta errado.
                        nome_suspeito = nome_strava
                        db.session.rollback()
                        break
                    if atleta.nome_atual != nome_strava:
                        atleta.nome_atual = nome_strava
                        nomes += 1
                db.session.commit()
            if nome_suspeito:
                logger.error(
                    'Atualizacao de perfis ABORTADA: o nome %r apareceu para '
                    '%d atletas diferentes na mesma passada. Isso e leitura '
                    'errada, nao coincidencia. Nenhum nome foi gravado.',
                    nome_suspeito, _MAX_MESMO_NOME)
                _andamento(
                    job_id, stage='done', done=True, ok=False, pct=100,
                    message=('PAREI POR SEGURANCA: o mesmo nome ("%s") veio '
                             'para %d atletas diferentes — isso e leitura '
                             'errada, nao coincidencia. Nenhum nome foi '
                             'alterado. As %d foto(s) ja atualizadas ficam.'
                             % (nome_suspeito, _MAX_MESMO_NOME, atualizadas)))
                return
            _andamento(
                job_id, stage='done', done=True, ok=True, pct=100,
                message=(('Concluído: %d foto(s) atualizada(s); %d atleta(s) '
                          'sem foto própria no Strava.' % (atualizadas, sem_foto))
                         if o_que == 'foto' else
                         ('Concluído: %d nome(s) atualizado(s); %d sem leitura '
                          'confiável (esses ficaram como estavam). Os nomes '
                          'escolhidos à mão não foram tocados.'
                          % (nomes, sem_foto))))
            # PASSADA COMPLETA: zera a fila e carimba a data -- e' o carimbo
            # que segura a proxima passada automatica ate vencer o prazo.
            try:
                from app import perfilauto
                perfilauto.marcar_concluida(cidade, o_que)
            except Exception as _e:
                logger.debug('Nao consegui marcar a passada: %s', _e)
        except Exception as exc:
            db.session.rollback()
            logger.error('Erro na atualizacao de fotos: %s', exc, exc_info=True)
            _andamento(job_id, stage='error', done=True, ok=False, pct=0,
                                 message=f'Erro: {exc}')


def tocar_atualizacao_de_perfis(cidade):
    """Continua (ou recomeca) a atualizacao de perfis desta cidade, sem clique.

    Chamada pelo vigia, de 15 em 15 minutos. Ela mesma decide se ha o que
    fazer -- e nao faz nada quando nao ha folga de cota, porque atualizar foto
    nunca pode ser o motivo de o ranking nao sair.

    E' o fecho da P-128: o dono pediu que "tudo funcione automatico sem clique
    para a frequencia definida e mesmo assim siga o fluxo de requisicoes
    permitidas". Nao se pede uma requisicao a mais por minuto; o que muda e'
    que o trabalho guarda de onde parou e volta sozinho.
    """
    from app import perfilauto
    if not perfilauto.ha_folga():
        return None
    # SEM COOKIE DO STRAVA NAO HA O QUE FAZER (17/09/2026). O job sai logo no
    # comeco com "Sessao do Strava nao configurada" — e sai ANTES de marcar a
    # passada como feita, entao a cidade era enfileirada de novo a cada 15
    # minutos, para sempre. A mesma checagem que o job faz, feita antes.
    try:
        from app import secretstore
        if not secretstore.get(secretstore.STRAVA_SESSION, scope=cidade):
            return None
    except Exception:
        return None

    # NAO EMPILHA: se ja ha uma atualizacao esperando ou rodando, nada a fazer.
    # A TRAVA DE PRIORIDADE QUE EU TINHA POSTO AQUI SAIU (07/09/2026, pedido do
    # dono). Ela mandava a manutencao ceder a vez para a importacao -- resolvia
    # o sintoma e criava uma regra nova para alguem manter. Com o trabalho
    # DENTRO da fila, a vaga unica ja garante uma requisicao por vez, a tela
    # mostra quem esta na frente, e nao ha prioridade a decidir.
    try:
        from app.blueprints.upload import _trabalhos_em_aberto
        for t in _trabalhos_em_aberto(cidade, todas_as_cidades=True):
            if t.get('kind') == 'atualizar_perfis':
                return None
    except Exception as exc:
        logger.debug('Nao consegui olhar a fila: %s', exc)
        return None          # na duvida, nao enfileira duas vezes

    for o_que in ('nome', 'foto'):
        if not perfilauto.vencida(cidade, o_que):
            continue
        fila = perfilauto.pendentes(cidade, o_que)
        # Fatia curta de proposito: o vigia volta em 15 minutos, e assim esta
        # rotina nunca ocupa a fila do Strava por muito tempo seguido.
        lote = fila[:perfilauto.POR_PASSADA] if fila else None
        resto = fila[perfilauto.POR_PASSADA:] if fila else None
        from app.blueprints.upload import enfileirar_atualizacao_de_perfis
        enfileirar_atualizacao_de_perfis(cidade, o_que, lote, automatico=True)
        if resto is not None:
            # O que nao coube nesta fatia continua guardado para a proxima.
            perfilauto.guardar_pendentes(cidade, o_que, resto, 'fatia do vigia')
        logger.info('Atualizacao automatica de %s iniciada em %s (%s)',
                    o_que, cidade,
                    '%d na fila' % len(lote) if lote else 'passada nova')
        return o_que        # um por passada: nao empilha trabalho
    return None


def _disparar_atualizacao(o_que):
    """Comeca a atualizacao de perfis em segundo plano. Devolve o job.

    As duas rotas abaixo so diferem no `o_que` -- o resto (CSRF, cidade,
    thread) e identico, e repetir isso duas vezes era o convite para as duas
    passarem a divergir na proxima mudanca.
    """
    erro = _validate_admin_csrf()
    if erro:
        return erro
    # VAI PARA A FILA, como upload e importacao (07/09/2026). Se houver
    # trabalho em andamento, este espera a vez e comeca sozinho -- e ate la
    # aparece na tela de upload dizendo que esta aguardando.
    from app.blueprints.upload import enfileirar_atualizacao_de_perfis
    job_id = enfileirar_atualizacao_de_perfis(
        current_user.cidade_ativa, o_que, admin_id=current_user.id)
    return jsonify(status='ok', job_id=job_id)


@admin_bp.route('/atletas/atualizar-fotos', methods=['POST'])
@login_required
@admin_required
def atualizar_fotos_atletas():
    """So a FOTO. Pula quem ja tem foto de menos de 30 dias."""
    return _disparar_atualizacao('foto')


@admin_bp.route('/atletas/atualizar-nomes', methods=['POST'])
@login_required
@admin_required
def atualizar_nomes_atletas():
    """So o NOME do Strava. Nao toca no nome escolhido para o ranking.

    Botao separado desde 06/09/2026: atualizar nome e 'e escrita em cima de
    dado bom (ja destruiu 62 registros uma vez), e a pessoa troca o nome no
    Strava quando quer -- nao ha janela de 30 dias que faca sentido aqui.
    """
    return _disparar_atualizacao('nome')


@admin_bp.route('/atletas/<strava_id>/sexo', methods=['POST'])
@login_required
@admin_required
def definir_sexo_atleta(strava_id):
    """Marca o sexo do atleta, na própria tabela, sem sair da página.

    POR QUE A MÃO, E POR QUE AQUI (09/09/2026, decisão do dono)

    O Strava NÃO publica esse dado. A documentação oficial diz com todas as
    letras que a escolha de gênero "will not appear on your profile page", e a
    medição confirmou por dois caminhos: com cookie vem o gênero da CONTA
    LOGADA — que é exatamente como 62 atletas ganharam o nome errado em 06/09
    (`ERROS.md` 4.39) —, e sem cookie não vem nada, embora nome e cidade
    venham. Não há o que raspar, e insistir só produziria dado errado.

    Fica na LISTA de atletas, e não numa tela à parte, porque é a tela que o
    dono abre toda semana: ele vê quem está sem preencher e resolve ali, em um
    clique, sem navegar para lugar nenhum.

    ⚠️ ESTE DADO NUNCA APARECE EM PÁGINA PÚBLICA por pessoa. Serve só para a
    contagem agregada da home (quantas atividades de homens, quantas de
    mulheres). Sexo de alguém não é dado de vitrine.
    """
    erro = _validate_admin_csrf()
    if erro:
        return erro

    valor = (request.form.get('sexo') or '').strip().upper()
    if valor not in ('M', 'F', ''):
        return jsonify(ok=False, erro='Valor inválido.'), 400

    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        return jsonify(ok=False, erro='Atleta não encontrado.'), 404

    atleta.sexo = valor or None
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error('Não consegui gravar o sexo de %s: %s', strava_id, exc)
        return jsonify(ok=False, erro='Não consegui salvar.'), 500

    logger.info('Sexo do atleta %s definido como %r por %s',
                strava_id, valor or '(vazio)', current_user.email)

    # DOIS CHAMADORES, DUAS RESPOSTAS. A lista de atletas chama por `fetch` e
    # espera JSON; a tela de ajuste e' formulario comum e precisa voltar para a
    # pagina com a mensagem. Devolver JSON ao formulario deixaria o dono
    # olhando um {"ok": true} na tela, sem entender o que aconteceu.
    if request.form.get('volta_para_tela'):
        flash('Sexo salvo.' if valor else 'Sexo marcado como não informado.',
              'success')
        return voltar('admin.ajuste_manual_atleta',
                                strava_id=strava_id)
    return jsonify(ok=True, sexo=valor)


@admin_bp.route('/atletas/<strava_id>/nome', methods=['POST'])
@login_required
@admin_required
def definir_nome_exibicao(strava_id):
    """Escolhe como este atleta aparece no ranking. Vazio volta ao do Strava.

    POR QUE ISTO EXISTE (29/08/2026, regra do dono). O nome que vinha do
    Strava nao tinha conserto: `nome_atual` e snapshot e qualquer link novo
    o reescrevia. A Ana Exemplo e conhecida como Ana Modelo; a Bia
    Exemplo, como Bia Modelo; o carlos escreveu o nome em minuscula. Nenhum
    deles vai pedir para mudar -- e agora nao precisa.

    O LIMITE DE DUAS PALAVRAS NAO E CAPRICHO: o ranking vai para o
    WhatsApp, e linha que passa da largura da tela quebra no meio do nome,
    desalinhando a tabela inteira.
    """
    erro = _validate_admin_csrf()
    if erro:
        return erro
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        flash('Atleta nao encontrado.', 'warning')
        return voltar('admin.lista_atletas')
    escolhido = ' '.join((request.form.get('nome_exibicao') or '').split())[:60]
    if escolhido and len(escolhido.split(' ')) > 3:
        flash('Use no máximo três palavras — nome comprido quebra a linha '
              'do ranking no WhatsApp e desalinha a tabela.', 'warning')
        return voltar('admin.ajuste_manual_atleta',
                                strava_id=strava_id)
    atleta.nome_exibicao = escolhido or None
    atleta.nome_exibicao_por = (getattr(current_user, 'email', '') or '')[:120]
    atleta.nome_exibicao_em = datetime.now()
    db.session.commit()
    # A foto do dia guarda os nomes como estavam; refazer aqui evita a
    # pagina publica mostrar o nome velho ate a proxima virada.
    try:
        from app import consolidado
        consolidado.refazer_foto_do_dia()
    except Exception as exc:
        logger.debug('Nao consegui refazer a foto do dia: %s', exc)
    if escolhido:
        logger.info('Nome de exibicao de %s definido como %r por %s',
                    strava_id, escolhido,
                    getattr(current_user, 'email', '?'))
        flash('No ranking esta pessoa passa a aparecer como "%s".'
              % escolhido, 'success')
    else:
        logger.info('Nome de exibicao de %s removido por %s', strava_id,
                    getattr(current_user, 'email', '?'))
        flash('Voltou a usar o nome do Strava.', 'success')
    return voltar('admin.ajuste_manual_atleta', strava_id=strava_id)


@admin_bp.route('/atletas')
@login_required
@viewer_required
def lista_atletas():
    """Lista de atletas da cidade — acessível a todos os usuários logados, somente leitura."""
    from datetime import datetime
    from sqlalchemy import func

    # ARQUIVADO SAI NO SQL, NAO SO NO CSS (29/08/2026).
    # A tela ja escondia o arquivado com `d-none`, mas ele vinha do banco em
    # toda visita e ainda contava no total -- o dono arquivava e continuava
    # vendo a mesma lista do mesmo tamanho. O checkbox "Mostrar arquivados"
    # continua funcionando: agora ele PEDE os arquivados ao servidor.
    mostrar_arquivados = request.args.get('arquivados') == '1'
    # Viewers e admins veem apenas atletas da sua cidade; master vê todos
    consulta = Atleta.query
    if not current_user.is_supremo:
        # Busca atletas que têm atividades na cidade do usuário
        strava_ids = db.session.query(Activity.id_usuario_strava).filter_by(
            cidade=current_user.cidade_ativa
        ).distinct().subquery()
        consulta = consulta.filter(Atleta.strava_id.in_(strava_ids))
    if not mostrar_arquivados:
        consulta = consulta.filter(db.or_(Atleta.arquivado.is_(None),
                                          Atleta.arquivado == False))
    atletas = consulta.order_by(Atleta.nome_atual).all()

    # Cidades de cada atleta (uma consulta só). Master vê todas; demais, a sua.
    cid_q = db.session.query(
        Activity.id_usuario_strava, Activity.cidade
    ).distinct()
    if not current_user.is_supremo:
        cid_q = cid_q.filter(Activity.cidade == current_user.cidade_ativa)
    cidades_por_atleta = {}
    for sid, cid in cid_q.all():
        if cid:
            cidades_por_atleta.setdefault(str(sid), set()).add(cid)

    # Conta atividades do mês atual por atleta (apenas cidade do usuário, ou todas para master)
    mes_ano = datetime.now().strftime('%Y-%m')
    cidade_filtro = None if current_user.is_supremo else current_user.cidade_ativa

    lista = [
        {
            'obj': a,
            'cidades': ', '.join(sorted(cidades_por_atleta.get(str(a.strava_id), []))),
        }
        for a in atletas
    ]

    return render_template(
        'admin/lista_atletas.html',
        atletas_info=lista,
        mes_ano=mes_ano,
        cidade_filtro=cidade_filtro,
        mostrar_arquivados=mostrar_arquivados,
        pode_arquivar=current_user.pode_administrar,
    )


@admin_bp.route('/atletas/<strava_id>/arquivar', methods=['POST'])
@login_required
@admin_required
def arquivar_atleta(strava_id):
    """Arquiva/desarquiva um perfil de atleta (duplicata/sem uso).

    Arquivado some das listas por padrao e mostra selo na pagina publica; volta
    sozinho quando uma atividade nova e processada para o strava_id -- A NAO
    SER que os quilometros tambem tenham sido tirados (`com_km`), caso em que
    a pessoa PEDIU para sair e o perfil NAO deve voltar sozinho (Fase 3 do
    plano das melhorias, 28/08/2026; `saiu_a_pedido`).

    `com_km`: alem do perfil, tira TODOS os quilometros deste strava_id de
    toda conta (ranking, pagina inicial, relatorios) -- `Activity.status =
    'removida'`, ja respeitado por toda soma do sistema. Marca
    `oculta_por_arquivamento=True` SO nas atividades que este botao mexeu,
    para desarquivar devolver exatamente essas e nao ressuscitar o que o
    anticheat rejeitou (mesmo valor de status, causa diferente).
    """
    erro = _validate_admin_csrf()
    if erro:
        return erro
    if not str(strava_id).isdigit():
        return jsonify(status='error', message='Strava ID invalido.'), 400
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        return jsonify(status='error', message='Atleta nao encontrado.'), 404
    acao = (request.form.get('acao') or 'arquivar').strip()
    com_km = (request.form.get('com_km') or '').strip() in ('1', 'true', 'sim')

    if acao == 'desarquivar':
        atleta.arquivado = False
        atleta.arquivado_em = None
        atleta.arquivado_motivo = None
        atleta.saiu_a_pedido = False
        msg = 'Perfil reativado.'
        if com_km:
            restauradas = Activity.query.filter_by(
                id_usuario_strava=str(strava_id), oculta_por_arquivamento=True
            ).update({'status': 'ativa', 'oculta_por_arquivamento': False},
                    synchronize_session=False)
            msg = f'Perfil reativado e {restauradas} atividade(s) voltaram a contar.'
    else:
        atleta.arquivado = True
        atleta.arquivado_em = datetime.now()
        atleta.arquivado_motivo = (request.form.get('motivo') or '').strip()[:200] or 'Perfil sem uso / duplicado'
        msg = 'Perfil arquivado.'
        if com_km:
            atleta.saiu_a_pedido = True
            ocultadas = Activity.query.filter(
                Activity.id_usuario_strava == str(strava_id),
                db.or_(Activity.status.is_(None), Activity.status != 'removida'),
            ).update({'status': 'removida', 'oculta_por_arquivamento': True},
                     synchronize_session=False)
            msg = f'Perfil arquivado e {ocultadas} atividade(s) saíram da conta.'
    db.session.commit()
    return jsonify(status='ok', message=msg, arquivado=atleta.arquivado)


@admin_bp.route('/atletas/mesclar', methods=['POST'])
@login_required
@admin_required
def mesclar_atletas():
    """Funde 2+ perfis de atleta duplicados num só (07/09/2026, pedido do dono).

    Km, ranking, anticheat e a pagina publica passam a enxergar o grupo como
    uma pessoa so (ver `Atleta.atleta_canonico_id` no modelo). O CABECA do
    grupo (quem fica com a identidade visivel) e' escolhido automaticamente:
    quem tem a atividade nao removida mais recente entre os selecionados --
    a mesma regra usada para destacar a tag do Strava na pagina publica.

    Sem "desfazer" pela tela -- e' um `UPDATE atletas SET atleta_canonico_id
    = NULL WHERE id = X` manual, se um dia precisar.
    """
    erro = _validate_admin_csrf()
    if erro:
        return erro
    ids = [int(i) for i in request.form.getlist('ids') if str(i).isdigit()]
    if len(ids) < 2:
        return jsonify(status='error', message='Selecione ao menos 2 atletas.'), 400

    # Achata o grupo: se algum selecionado ja e' cabeca (ou membro) de um
    # grupo existente, traz todo mundo junto -- nunca cadeias A->B->C.
    grupo_ids = set()
    for a in Atleta.query.filter(Atleta.id.in_(ids)).all():
        cid = a.atleta_canonico_id or a.id
        for m in Atleta.query.filter(
                db.or_(Atleta.id == cid, Atleta.atleta_canonico_id == cid)).all():
            grupo_ids.add(m.id)
    membros = Atleta.query.filter(Atleta.id.in_(grupo_ids)).all()
    if len(membros) < 2:
        return jsonify(status='error', message='Selecione ao menos 2 atletas.'), 400

    strava_ids = [m.strava_id for m in membros]
    ultima = (
        db.session.query(Activity.id_usuario_strava)
        .filter(Activity.id_usuario_strava.in_(strava_ids), Activity.status != 'removida')
        .order_by(Activity.data_hora.desc())
        .first()
    )
    vencedor_sid = ultima[0] if ultima else strava_ids[0]
    canonico = next((m for m in membros if m.strava_id == vencedor_sid), membros[0])

    for m in membros:
        if m.id == canonico.id:
            m.atleta_canonico_id = None
            continue
        m.atleta_canonico_id = canonico.id
        m.mesclado_em = datetime.now()
        m.mesclado_por = getattr(current_user, 'email', '') or ''
        m.arquivado = True
        m.arquivado_motivo = f'Mesclado com {canonico.nome_atual} (strava_id {canonico.strava_id})'
    db.session.commit()
    logger.info('Atletas mesclados em %s (strava_id %s) por %s: %s',
                canonico.nome_atual, canonico.strava_id,
                getattr(current_user, 'email', '?'), strava_ids)
    return jsonify(status='ok',
                    message=f'{len(membros)} perfis fundidos em {canonico.nome_atual}.')


def _nome_no_ranking(strava_id, nome_do_strava):
    """Como esta pessoa aparece HOJE no ranking -- para a tela mostrar.

    O admin nao tem como adivinhar o que `_nome_curto_atleta` vai escolher
    (primeiro nome + sobrenome mais curto, sem emoji nem tipo sanguineo).
    Mostrar o resultado ao lado do campo evita que ele digite um nome novo
    so porque nao sabia qual seria o automatico.
    """
    try:
        from app.blueprints.ranking import _nome_curto_atleta
        atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
        escolhido = (getattr(atleta, 'nome_exibicao', '') or '').strip()
        return escolhido or _nome_curto_atleta(nome_do_strava)
    except Exception:
        return nome_do_strava or ''


@admin_bp.route('/atletas/<strava_id>/ajuste-manual')
@login_required
@admin_required
def ajuste_manual_atleta(strava_id):
    """Tela administrativa para adicionar km manual sem expor o formulario no painel publico."""
    if not str(strava_id).isdigit() or len(str(strava_id)) > 20:
        abort(404)

    def _normalizar_nome(nome):
        return ' '.join((nome or '').strip().lower().split())

    name_key = _normalizar_nome(request.args.get('name_key'))
    # Atleta mesclado (07/09/2026): a tela mostra o km e os dados do GRUPO
    # inteiro, nao so' deste strava_id -- mesmo que o link aberto seja o de
    # um perfil que foi fundido em outro.
    atleta_url = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    atleta_obj = Atleta.resolver_canonico(atleta_url) if atleta_url else None
    strava_ids_grupo = atleta_obj.strava_ids_do_grupo() if atleta_obj else [str(strava_id)]
    query = Activity.query.filter(Activity.id_usuario_strava.in_(strava_ids_grupo))
    atividades = query.order_by(Activity.data_hora.desc()).all()
    if str(strava_id) == '0' and name_key:
        atividades = [a for a in atividades if _normalizar_nome(a.nome_usuario) == name_key]

    if not current_user.is_supremo:
        atividades = [a for a in atividades if a.cidade == current_user.cidade_ativa]

    if not atividades:
        flash('Nenhuma atividade encontrada para este atleta na sua cidade.', 'warning')
        return redirect(url_for('admin.lista_atletas'))

    cidades = sorted({a.cidade for a in atividades if a.cidade})
    cidade_url = (request.args.get('cidade') or '').strip()
    if current_user.is_supremo and cidade_url in cidades:
        cidade_selecionada = cidade_url
    elif current_user.cidade_ativa in cidades:
        cidade_selecionada = current_user.cidade_ativa
    else:
        cidade_selecionada = cidades[0] if cidades else current_user.cidade_ativa

    now = datetime.now()
    atividades_mes = [
        a for a in atividades
        if (
            a.cidade == cidade_selecionada and
            a.status != 'removida' and
            a.data_hora and
            a.data_hora.year == now.year and
            a.data_hora.month == now.month
        )
    ]
    km_corrida_mes = round(sum(a.distancia or 0 for a in atividades_mes if a.modalidade == 'corrida_caminhada'), 2)
    km_ciclismo_mes = round(sum(a.distancia or 0 for a in atividades_mes if a.modalidade == 'ciclismo'), 2)

    return render_template(
        'admin/athlete_adjustment.html',
        atleta_nome=atividades[0].nome_usuario,
        strava_id=strava_id,
        name_key=name_key,
        cidades=cidades,
        cidade_selecionada=cidade_selecionada,
        atividades_recentes=atividades[:10],
        km_corrida_mes=km_corrida_mes,
        km_ciclismo_mes=km_ciclismo_mes,
        month=now.month,
        year=now.year,
        today=now.strftime('%Y-%m-%d'),
        # Conferência automática: este atleta está fora NESTE mês? (27/08/2026)
        conf_dispensado=_conferencia_dispensado(cidade_selecionada, strava_id,
                                                now.year, now.month),
        conf_mes_nome=_NOMES_MES.get(now.month, ''),
        # Reserva nas paginas publicas (28/08/2026)
        reservado_no_publico=bool(getattr(atleta_obj, 'oculto_no_publico', False)),
        # O nome escolhido para o ranking (29/08/2026). Vazio = usa o do
        # Strava, e a tela mostra qual seria.
        nome_exibicao_atual=(getattr(atleta_obj, 'nome_exibicao', '') or ''),
        nome_no_ranking_hoje=_nome_no_ranking(strava_id,
                                              atividades[0].nome_usuario),
        # Tipo de bicicleta (Fase 4 do plano das melhorias, 28/08/2026)
        tipo_bicicleta_atual=(getattr(atleta_obj, 'tipo_bicicleta', None) or 'mtb'),
        sexo_atual=(getattr(atleta_obj, 'sexo', None) or ''),
        atleta_mesclado=(atleta_obj.strava_id != str(strava_id)) if atleta_obj else False,
        strava_id_canonico=(atleta_obj.strava_id if atleta_obj else str(strava_id)),
    )


def _conferencia_dispensado(cidade, strava_id, ano, mes):
    """Wrapper que nunca derruba a tela por causa da conferência."""
    try:
        from app import conferenciaauto
        return conferenciaauto.dispensado(cidade, strava_id, ano, mes)
    except Exception as exc:
        logger.debug('conferencia: nao consegui ler a dispensa de %s: %s', strava_id, exc)
        return False


_NOMES_MES = {1: 'janeiro', 2: 'fevereiro', 3: 'março', 4: 'abril', 5: 'maio',
              6: 'junho', 7: 'julho', 8: 'agosto', 9: 'setembro',
              10: 'outubro', 11: 'novembro', 12: 'dezembro'}


@admin_bp.route('/atletas/<strava_id>/privacidade', methods=['POST'])
@login_required
@admin_required
def alternar_privacidade_atleta(strava_id):
    """Liga/desliga a reserva do atleta nas paginas publicas.

    POR QUE ISTO EXISTE (regra do dono, 28/08/2026)
    A pagina inicial passou a mostrar nome e quilometros de quem participa. Nem
    todo mundo quer isso, e a pessoa tem o direito de escolher -- e' o minimo.
    Marcado aqui, o nome dela some da pagina inicial, da consolidada e das
    listas publicas, e `/ath/<id>` deixa de existir para quem nao esta logado.

    ⚠️ OS QUILOMETROS FICAM. Ela continua no ranking do grupo, continua
    recebendo medalha e continua somando no total do projeto: o que se pede aqui
    e' anonimato na vitrine, nao sair do projeto.
    """
    if not str(strava_id).isdigit():
        abort(404)
    reservar = (request.form.get('reservar') or '').strip() == '1'
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        flash('Não encontrei o cadastro deste atleta.', 'warning')
        return voltar('admin.lista_atletas')
    if not current_user.is_supremo and atleta.strava_id:
        # admin de cidade so mexe em quem tem atividade na cidade dele
        tem = Activity.query.filter_by(
            id_usuario_strava=str(strava_id), cidade=current_user.cidade_ativa).first()
        if not tem:
            abort(403)
    atleta.oculto_no_publico = reservar
    db.session.commit()
    try:
        from app.consolidado import refazer_foto_do_dia
        refazer_foto_do_dia()
    except Exception as exc:
        logger.warning('nao consegui refazer a foto publica: %s', exc)
    flash('Este atleta não aparece mais nas páginas públicas.' if reservar
          else 'Este atleta voltou a aparecer nas páginas públicas.', 'success')
    return voltar('admin.ajuste_manual_atleta', strava_id=strava_id,
                            cidade=request.form.get('cidade') or '')


_TIPOS_BICICLETA = ('speed', 'mtb', 'gravel')


@admin_bp.route('/atletas/<strava_id>/bicicleta', methods=['POST'])
@login_required
@admin_required
def alternar_bicicleta_atleta(strava_id):
    """Marca o tipo de bicicleta do atleta -- speed/mtb/gravel (Fase 4, 28/08/2026).

    Comparar Speed com MTB e' injusto: a bicicleta explica boa parte da
    diferenca de velocidade E de quilometragem (quem anda de Speed cobre
    mais km no mesmo tempo). Aparece como selo ao lado do nome, na tabela de
    km E na de mais veloz -- sem separar tabela nenhuma.
    """
    if not str(strava_id).isdigit():
        abort(404)
    tipo = (request.form.get('tipo_bicicleta') or '').strip().lower()
    if tipo not in _TIPOS_BICICLETA:
        flash('Tipo de bicicleta inválido.', 'error')
        return voltar('admin.ajuste_manual_atleta', strava_id=strava_id,
                                cidade=request.form.get('cidade') or '')
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        flash('Não encontrei o cadastro deste atleta.', 'warning')
        return voltar('admin.lista_atletas')
    if not current_user.is_supremo and atleta.strava_id:
        tem = Activity.query.filter_by(
            id_usuario_strava=str(strava_id), cidade=current_user.cidade_ativa).first()
        if not tem:
            abort(403)
    atleta.tipo_bicicleta = tipo
    db.session.commit()
    try:
        from app.consolidado import refazer_foto_do_dia
        refazer_foto_do_dia()
    except Exception as exc:
        logger.warning('nao consegui refazer a foto publica: %s', exc)
    flash(f'Bicicleta marcada como {tipo}.', 'success')
    return voltar('admin.ajuste_manual_atleta', strava_id=strava_id,
                            cidade=request.form.get('cidade') or '')


@admin_bp.route('/atletas/<strava_id>/conferencia-dispensa', methods=['POST'])
@login_required
@admin_required
def alternar_dispensa_conferencia(strava_id):
    """Tira (ou devolve) o atleta da conferência automática DESTE mês.

    POR QUE ISTO EXISTE (pedido do dono, 26/08/2026)
    A conferência traz para o ranking atividade que está no Strava e não foi
    postada no grupo — é a contingência contra a coleta perder link. Mas há
    quem, num mês, simplesmente NÃO QUEIRA participar (uma atleta e um atleta
    pediram isso em agosto/2026). **Quem não quer participar, não participa**:
    isso é regra, não conveniência.

    VALE SÓ NAQUELE MÊS, de propósito. Se no mês seguinte a pessoa voltar a
    postar, ela volta a ser conferida sozinha — ninguém precisa lembrar de
    desmarcar nada.
    """
    csrf_err = _validate_admin_csrf()
    if csrf_err:
        return csrf_err
    if not str(strava_id).isdigit():
        abort(404)

    from app import conferenciaauto
    cidade = (request.form.get('cidade') or current_user.cidade_ativa or '').strip()
    if not current_user.is_supremo and cidade != current_user.cidade_ativa:
        abort(403)          # fronteira de cidade e regra de seguranca (regra 1)

    agora = datetime.now()
    fora = (request.form.get('fora') or '') == '1'
    conferenciaauto.dispensar(cidade, strava_id, agora.year, agora.month,
                              por=current_user.email, dispensar_=fora)
    logger.warning('Conferencia: atleta %s %s de %s/%d por %s', strava_id,
                   'DISPENSADO' if fora else 'devolvido', agora.month, agora.year,
                   current_user.email)
    flash(
        ('Pronto. Este atleta não será conferido em %s/%d — nada dele entra no '
         'ranking sem ser postado no grupo.' % (_NOMES_MES.get(agora.month, ''), agora.year))
        if fora else
        ('Este atleta volta a ser conferido em %s/%d.'
         % (_NOMES_MES.get(agora.month, ''), agora.year)),
        'success')
    return voltar('admin.ajuste_manual_atleta', strava_id=strava_id,
                            cidade=cidade)


@admin_bp.route('/atividade/<int:activity_id>/alternar-contagem', methods=['POST'])
@login_required
@admin_required
def alternar_contagem_atividade(activity_id):
    """Marca/desmarca uma atividade para contar no ranking (pedido do dono, 22/08/2026).

    O mecanismo (Activity.status = 'ativa'/'removida') ja existe e ja e
    respeitado por toda soma de km do ranking -- ate hoje so era alcancavel
    via admin.rejeitar_revisao, que exige uma FlagAnticheat existir primeiro.
    Este botao da o mesmo poder direto na tela de ajuste manual do atleta,
    sem exigir flag: e uma acao deliberada do admin, dispensa justificativa
    por escrito (diferente da revisao de anticheat, que e um processo formal).
    """
    atividade = Activity.query.get_or_404(activity_id)
    if not current_user.is_supremo and atividade.cidade != current_user.cidade_ativa:
        abort(403)

    corpo = request.get_json(silent=True) or {}
    valor = request.form.get('contar', corpo.get('contar'))
    contar = valor in ('1', 1, 'true', True)

    atividade.status = 'ativa' if contar else 'removida'
    if atividade.data_hora:
        from app.blueprints.anticheat import recalcular_controle_disciplinar
        atleta = Atleta.query.filter_by(strava_id=atividade.id_usuario_strava).first()
        if atleta:
            recalcular_controle_disciplinar(atleta.id, atividade.data_hora.strftime('%Y-%m'))
    db.session.commit()
    logger.info('Atividade %s (%s, %s) marcada como %s por %s',
                atividade.id_atividade, atividade.nome_usuario, atividade.cidade,
                atividade.status, current_user.email)
    return jsonify(status='ok', contando=(atividade.status == 'ativa'))


@admin_bp.route('/recalcular_ranking', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): reconsolidar o ranking DA PROPRIA cidade e do admin tambem
def recalcular_ranking():
    from app.models import Consolidacao, Activity, FlagAnticheat, ControleDisciplinar, Atleta
    from datetime import datetime
    from collections import defaultdict
    cidade = current_user.cidade_ativa
    mes_ano = datetime.now().strftime('%Y-%m')
    # Remover flags PADRAO_SUSPEITO (invalidas) e corrigir CD
    flags_ps = FlagAnticheat.query.filter_by(tipo_flag='PADRAO_SUSPEITO').all()
    afetados = {}
    for f in flags_ps:
        afetados.setdefault(f.atleta_id, 0)
        afetados[f.atleta_id] += 1
        db.session.delete(f)
    for aid, cnt in afetados.items():
        cd = ControleDisciplinar.query.filter_by(atleta_id=aid, mes_ano=mes_ano).first()
        if cd:
            cd.total_flags = max(0, (cd.total_flags or 0) - cnt)
            cd.avisos = max(0, (cd.avisos or 0) - cnt)
    db.session.commit()
    n_flags = len(flags_ps)
    # Recalcular Consolidacao
    Consolidacao.query.filter_by(periodo=mes_ano, tipo='mensal').delete()
    acts = (Activity.query.filter_by(cidade=cidade)
            .filter(Activity.status == 'ativa').all())
    rejected_ids = {f.atividade_id for f in FlagAnticheat.query.filter_by(status='rejeitada').all()}
    stats = defaultdict(lambda: {'km_run': 0, 'km_bike': 0, 'count': 0})
    for act in acts:
        if act.id in rejected_ids:
            continue
        if not act.data_hora or act.data_hora.strftime('%Y-%m') != mes_ano:
            continue
        atleta = Atleta.query.filter_by(strava_id=act.id_usuario_strava).first()
        if not atleta:
            continue
        atleta = Atleta.resolver_canonico(atleta)
        if act.modalidade == 'corrida_caminhada':
            stats[atleta.id]['km_run'] += act.distancia or 0
        elif act.modalidade == 'ciclismo':
            stats[atleta.id]['km_bike'] += act.distancia or 0
        stats[atleta.id]['count'] += 1
    for aid, s in stats.items():
        c = Consolidacao(atleta_id=aid, periodo=mes_ano, tipo='mensal',
                         km_run=round(s['km_run'], 2), km_bike=round(s['km_bike'], 2),
                         km_total=round(s['km_run'] + s['km_bike'], 2),
                         atividades_count=s['count'])
        db.session.add(c)
    db.session.commit()
    total_run = sum(s['km_run'] for s in stats.values())
    total_bike = sum(s['km_bike'] for s in stats.values())
    flash('Ranking recalculado: {:.1f} km corrida, {:.1f} km ciclismo. {} flags PADRAO_SUSPEITO removidas.'.format(
        total_run, total_bike, n_flags), 'success')
    return voltar('admin.dashboard')


# ===================== FECHAMENTO DE MES E BACKUPS =====================
# O mes fechado e a versao oficial daquele periodo: medalhistas, patrocinador
# e texto do ranking congelados. Os backups automaticos moram na mesma tela
# porque respondem a mesma pergunta do operador: "o que ja esta garantido?".

@admin_bp.route('/fechamento')
@login_required
@admin_required
def fechamento():
    """Meses fechados da cidade + situacao dos backups automaticos."""
    from app import fechamento as fech
    from app import backupauto
    from app.blueprints.ranking import MESES_PT

    cidade = current_user.cidade_ativa
    agora = datetime.now()
    fechados = fech.listar(cidade)
    ja = {(f['ano'], f['mes']) for f in fechados}

    # Meses com ranking que ainda estao abertos (candidatos a fechar).
    from app.models import MonthlyBackup
    abertos = []
    for mb in (MonthlyBackup.query.filter_by(cidade=cidade)
               .order_by(MonthlyBackup.ano.desc(), MonthlyBackup.mes.desc())
               .limit(24).all()):
        if (mb.ano, mb.mes) in ja:
            continue
        abertos.append({
            'ano': mb.ano, 'mes': mb.mes,
            'corrente': (mb.ano == agora.year and mb.mes == agora.month),
            'total_atletas': mb.total_atletas or 0,
            'total_medalhas': mb.total_medalhas or 0,
        })

    return render_template('admin/fechamento.html',
                           cidade=cidade, fechados=fechados, abertos=abertos,
                           meses=MESES_PT, carencia=fech.DIAS_DE_CARENCIA,
                           backups=backupauto.situacao())


@admin_bp.route('/fechamento/fechar', methods=['POST'])
@login_required
@admin_required
def fechamento_fechar():
    from app import fechamento as fech
    from app.blueprints.ranking import MESES_PT
    cidade = current_user.cidade_ativa
    ano = request.form.get('ano', type=int)
    mes = request.form.get('mes', type=int)
    if not ano or not mes:
        flash('Período inválido.', 'warning')
        return voltar('admin.fechamento')
    try:
        dados = fech.fechar_mes(cidade, ano, mes, fechado_por=current_user.email)
    except Exception as exc:
        db.session.rollback()
        logger.error('Falha ao fechar %s %s/%s: %s', cidade, mes, ano, exc, exc_info=True)
        flash(f'Não consegui fechar o mês: {exc}', 'error')
        return voltar('admin.fechamento')
    if not dados:
        flash(f'{MESES_PT.get(mes, mes)}/{ano} não tem ranking para fechar.', 'warning')
    else:
        flash(f"{MESES_PT.get(mes, mes)}/{ano} fechado: {dados['total_medalhas']} medalha(s), "
              f"patrocinador \"{dados['patrocinador']}\".", 'success')
    return voltar('admin.fechamento')


@admin_bp.route('/fechamento/reabrir', methods=['POST'])
@login_required
@supremo_required
def fechamento_reabrir():
    """Reabrir e privilegio do dono da instalacao: desfaz um compromisso."""
    from app import fechamento as fech
    from app.blueprints.ranking import MESES_PT
    cidade = current_user.cidade_ativa
    ano = request.form.get('ano', type=int)
    mes = request.form.get('mes', type=int)
    if not ano or not mes:
        flash('Período inválido.', 'warning')
        return voltar('admin.fechamento')
    fech.reabrir_mes(cidade, ano, mes, por=current_user.email)
    flash(f'{MESES_PT.get(mes, mes)}/{ano} reaberto. Feche de novo depois de corrigir.', 'warning')
    return voltar('admin.fechamento')


@admin_bp.route('/backup-automatico/rodar', methods=['POST'])
@login_required
@supremo_required
def backup_automatico_rodar():
    """Dispara agora o backup agendado (config ou codigo)."""
    from app import backupauto
    qual = (request.form.get('qual') or '').strip()
    if qual not in ('config', 'codigo'):
        flash('Escolha o que deseja copiar.', 'warning')
        return voltar('admin.fechamento')
    feitos = backupauto.rodar_backups_agendados(forcar=qual)
    if feitos.get(qual):
        flash(f'Backup de {qual} gerado: {os.path.basename(feitos[qual])}', 'success')
    else:
        flash('O backup não rodou agora (outro processo estava fazendo, ou houve erro — veja o log).', 'warning')
    return voltar('admin.fechamento')


# ===================== TELEFONES DOS ATLETAS =====================
# REGRA DOS ULTIMOS LINKS: o telefone de um atleta sai do cabecalho das
# mensagens que postaram os links MAIS RECENTES dele, por maioria. Funciona porque quem posta o proprio
# link posta do proprio numero.
#
# A ARMADILHA: em treino de grupo uma pessoa so posta os links de varios
# atletas. O numero dela viraria o telefone de todo mundo — foi assim que um
# unico numero apareceu em 58 atletas na coluna antiga. Por isso, quando um
# telefone e candidato de mais de um atleta, ele NAO e aplicado: entra na
# lista de ambiguos para o admin decidir olhando nome por nome.
#
# Quantos links recentes entram na votacao. Cinco cobre uma semana tipica
# de quem corre e ainda dilui o dia em que alguem postou pelo outro.
@admin_bp.route('/telefones', methods=['GET', 'POST'])
@login_required
@admin_required
def telefones_atletas():
    """A agenda telefonica: uma tabela, um botao.

    POR QUE ESTA TELA ENCOLHEU (06/08/2026)
    Ela tinha seis tabelas — candidatos, ambiguos, divergentes, resolvidos,
    orfaos, fora do padrao — e cada uma com um paragrafo explicando a regra. Na
    hora em que o admin precisou de fato usar (corrigir o numero de uma atleta na
    mao), a tela quebrou. Explicacao que atrapalha o uso nao e documentacao, e
    entulho: o que ele precisa e achar a pessoa, ver o numero e trocar.

    Ficaram duas coisas: a lista com Salvar em cada linha, e o botao que aplica
    a regra dos 5 links mais recentes.
    """
    from app import agendatelefone

    cidade = current_user.cidade_ativa

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()

        if acao == 'salvar':
            # Dois botoes na mesma linha: Salvar manda `chave`, Apagar manda
            # `apagar`. Apagar e salvar com o telefone em branco — um caminho
            # so, sem rota nova e sem chance de os dois divergirem.
            apagar = (request.form.get('apagar') or '').strip()
            usar = (request.form.get('usar') or '').strip()
            usar_troca = (request.form.get('usar_troca') or '').strip()
            chave = (apagar or usar or usar_troca
                     or (request.form.get('chave') or '').strip())
            if apagar:
                telefone, email = '', request.form.get('email_%s' % apagar)
            elif usar_troca:
                telefone = request.form.get('troca_%s' % usar_troca)
                email = request.form.get('email_%s' % usar_troca)
            elif usar:
                # "usar" aceita o que a conversa aponta. Continua sendo decisao
                # de gente — so poupa a digitacao.
                telefone = request.form.get('sugestao_%s' % usar)
                email = request.form.get('email_%s' % usar)
            else:
                telefone = request.form.get('telefone_%s' % chave)
                email = request.form.get('email_%s' % chave)
            if chave.startswith('u'):
                mensagem, tipo = _salvar_telefone_de_usuario(chave[1:], telefone, email)
            else:
                mensagem, tipo = agendatelefone.gravar_telefone(
                    chave, telefone, email, por=current_user.email)
            flash(mensagem, tipo)
            return voltar('admin.telefones_atletas')

        if acao == 'copia':
            lote, erro = agendatelefone.salvar_copia(current_user.email)
            flash(('Não consegui guardar a cópia: %s' % erro) if erro else
                  'Cópia guardada (%s). Dá para voltar a ela a qualquer momento.' % lote,
                  'danger' if erro else 'success')
            return voltar('admin.telefones_atletas')

        if acao == 'restaurar':
            relatorio, erro = agendatelefone.restaurar(
                (request.form.get('lote') or '').strip(), current_user.email)
            if erro:
                flash(erro, 'danger')
            else:
                flash('Voltei %d telefone(s) para como estavam em %s. '
                      '(%d já estavam iguais.)'
                      % (relatorio['voltaram'], relatorio['lote'], relatorio['iguais']),
                      'success')
            return voltar('admin.telefones_atletas')

        if acao == 'atualizar':
            # Le as conversas que faltarem e aplica a regra dos 5 links. Uma
            # coisa so, um clique so: para quem usa, "ler" e "aplicar" sao a
            # mesma vontade.
            resumo = agendatelefone.varrer(
                current_app.config['UPLOAD_FOLDER_TXT'], limite_arquivos=8)
            # Por padrao SO PREENCHE quem esta sem numero. Trocar o que ja
            # existe desfaria a conferencia feita na mao — e a prova sai das
            # mesmas conversas que geraram os numeros errados.
            relatorio, erro = agendatelefone.aplicar(
                cidade, so_fortes=True,
                trocar_existentes=request.form.get('trocar') == '1',
                por=current_user.email)
            if erro:
                flash(erro, 'danger')
                return voltar('admin.telefones_atletas')
            partes = []
            if relatorio['gravados']:
                partes.append('%d número(s) corrigido(s) pela prova' % relatorio['gravados'])
            if relatorio['formato']:
                partes.append('%d escrito(s) no formato certo' % relatorio['formato'])
            if relatorio['apagados_de_outro']:
                partes.append('%d apagado(s) por serem de outra pessoa'
                              % relatorio['apagados_de_outro'])
            if relatorio['apagados_perdedores']:
                partes.append('%d apagado(s) por serem perfil repetido'
                              % relatorio['apagados_perdedores'])
            if relatorio.get('preservados'):
                partes.append('%d mantido(s) como estão (a prova discorda, mas '
                              'número já preenchido não é trocado sozinho)'
                              % relatorio['preservados'])
            flash(('Pronto: %s.' % ', '.join(partes)) if partes
                  else 'Nada a preencher: quem tinha prova já tem número.',
                  'success' if partes else 'info')
            if resumo.get('arquivos_restantes'):
                flash('Ainda faltam %d arquivo(s) de conversa para ler — clique '
                      'de novo para terminar.' % resumo['arquivos_restantes'], 'info')
            if relatorio['recusados']:
                flash('%d não deu para gravar; corrija na mão pela tabela.'
                      % len(relatorio['recusados']), 'warning')
            return voltar('admin.telefones_atletas')

        return voltar('admin.telefones_atletas')

    agendatelefone.ensure_schema()
    linhas = agendatelefone.agenda(cidade)

    # QUEM VAI PERDER A MEDALHA POR FALTA DE NUMERO (P-25).
    # Esta e a tela onde se conserta telefone, entao e aqui que a falta tem de
    # aparecer. Em 01/08 sairam 10 avisos de 12 medalhistas: os dois de fora
    # nao tinham numero, e nada no sistema apontava para isso — a rodada dizia
    # que tinha enviado, e tinha mesmo, tudo que dava.
    # Falhar aqui nao pode derrubar a agenda: e um aviso, nao a funcao da tela.
    medalha_ano, medalha_mes, medalha_sem_fone = None, None, []
    try:
        from app import mensagens
        medalha_ano, medalha_mes = mensagens.mes_da_medalha()
        medalha_sem_fone = mensagens.medalhistas_sem_telefone(
            cidade, medalha_ano, medalha_mes)
    except Exception as exc:
        logger.warning('Nao consegui apurar medalhistas sem telefone (%s): %s',
                       cidade, exc)

    return render_template('admin/telefones.html', cidade=cidade, linhas=linhas,
                           copias=agendatelefone.copias(),
                           com_telefone=sum(1 for l in linhas if l['telefone']),
                           medalha_sem_fone=medalha_sem_fone,
                           medalha_ano=medalha_ano, medalha_mes=medalha_mes)


def _salvar_telefone_de_usuario(user_id, telefone, email):
    """Mesma gravacao, para quem loga no site. (mensagem, categoria)."""
    from app.telefone import normalizar, bonito

    usuario = Usuario.query.get(int(user_id)) if str(user_id).isdigit() else None
    if not usuario:
        return 'Usuário não encontrado.', 'warning'
    numero = normalizar(telefone) if (telefone or '').strip() else None
    if (telefone or '').strip() and not numero:
        return 'Não entendi o telefone. Escreva com DDD, sem o +55.', 'warning'
    try:
        usuario.telefone = numero
        if (email or '').strip():
            usuario.email = email.strip()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return 'Não consegui gravar: %s' % str(exc)[:120], 'danger'
    return ('%s: %s.' % (usuario.nome, bonito(numero) if numero else 'telefone apagado'),
            'success')


@admin_bp.route('/telefones/exportar')
@login_required
@admin_required
def exportar_telefones():
    """A agenda em CSV, pronta para virar contato no celular.

    GUARDADO SEM O "+", EXPORTADO COM ELE (decisao do dono, 06/08). Dentro do
    sistema o numero e 55DDD9XXXXXXXX — a forma que o WAHA e a fila de mensagens
    aceitam, e a que nao pede nada de quem digita: o adm poe DDD e numero e
    pronto, sem +55, porque a aplicacao ainda nao atende fora do Brasil. No
    arquivo que vira contato, o formato internacional e o que faz o aparelho
    reconhecer o pais.
    """
    from app import agendatelefone
    from flask import Response

    cidade = current_user.cidade_ativa
    linhas = ['Nome,Telefone,Perfil no Strava']
    for nome, internacional, legivel, strava_id in agendatelefone.exportar_contatos(cidade):
        limpo = (nome or '').replace('"', "'").replace(',', ' ')
        linhas.append('%s,%s,%s' % (limpo, internacional, strava_id))
    corpo = '\r\n'.join(linhas) + '\r\n'
    nome_arquivo = 'agenda-%s-%s.csv' % (
        re.sub(r'[^a-zA-Z0-9]+', '-', cidade or 'cidade').strip('-').lower(),
        datetime.now().strftime('%Y%m%d'))
    # BOM: o Excel em portugues abre CSV sem BOM com os acentos quebrados.
    return Response('﻿' + corpo, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':
                             'attachment; filename="%s"' % nome_arquivo})


# ===================== ETIQUETAS DAS MEDALHAS =====================
# Folha A4 com 80 etiquetas de 12,7 x 44,45 mm (4 colunas x 20 linhas) — o
# formato das etiquetas adesivas comuns de papelaria.
#
# DOIS DETALHES QUE PARECEM BOBOS E NAO SAO:
#   1. Folha comecada. Quase nunca se imprime 80 de uma vez; a folha volta
#      para a gaveta com metade das etiquetas usadas. Por isso da para pular
#      N posicoes e comecar de onde a folha parou.
#   2. Controle do que ja foi impresso. Sem isso, na segunda rodada ninguem
#      lembra quem ja tem etiqueta e sai medalha repetida.

# O PAPEL E CARTA, NAO A4. Isto era um defeito ate 07/08/2026: a folha estava
# montada em A4 (210 x 297) e as etiquetas Pimaco sao para CARTA (215,9 x
# 279,4). Impresso assim, tudo saia 8,8 mm baixo e 3 mm para dentro — ou seja,
# o nome caindo fora do adesivo, que e exatamente o que nao pode acontecer.
PAPEL_CARTA_MM = (215.9, 279.4)

# AS MEDIDAS SAO OS PARAMETROS OFICIAIS DA PIMACO, NAO CONTA MINHA.
#
# O erro que isto corrige (07/08/2026): eu tinha CALCULADO as margens supondo
# as colunas coladas umas nas outras e a grade centralizada no papel. Nao e
# assim. A Pimaco publica:
#   margem superior 12,7 mm · margem esquerda 14,5 mm
#   passo horizontal 47,5 mm  (a etiqueta tem 44,45 -> ha 3,05 mm de VAO)
#   passo vertical = a propria altura da etiqueta (sem vao entre linhas)
# Com a minha conta, a margem esquerda saia 19,05 e as colunas iam se
# desalinhando 3 mm a cada coluna — a quarta coluna caia 9 mm fora do adesivo.
#
# A conta so serve para CONFERIR, nunca para definir: 14,5 + 3x47,5 + 44,45 =
# 201,45, sobrando 14,45 a direita; 12,7 + 20x12,7 = 266,7, sobrando 12,7
# embaixo. Bate simetrico nos dois modelos, o que confirma os numeros.
MODELOS_ETIQUETA = {
    '6087': {
        'nome': 'Pimaco 6087 — 80 por folha (12,7 × 44,45 mm)',
        'colunas': 4, 'linhas': 20, 'largura': 44.45, 'altura': 12.7,
        'fonte_nome': 8.2, 'fonte_detalhe': 6.2,
    },
    '6089': {
        'nome': 'Pimaco 6089 — 60 por folha (16,93 × 44,45 mm)',
        'colunas': 4, 'linhas': 15, 'largura': 44.45, 'altura': 16.93,
        # Etiqueta mais alta cabe letra maior — e nome de atleta e para ser
        # lido de longe, colado numa medalha.
        'fonte_nome': 9.5, 'fonte_detalhe': 7.0,
    },
}
MODELO_ETIQUETA_PADRAO = '6087'

# Iguais nos dois modelos (parametros Pimaco para papel Carta).
ETIQUETA_MARGEM_TOPO_MM = 12.7
ETIQUETA_MARGEM_ESQUERDA_MM = 14.5
ETIQUETA_PASSO_HORIZONTAL_MM = 47.5


def _modelo_etiqueta(codigo=None):
    """O modelo pedido, com as medidas oficiais e o vao entre colunas."""
    codigo = (codigo or '').strip() or MODELO_ETIQUETA_PADRAO
    base = MODELOS_ETIQUETA.get(codigo) or MODELOS_ETIQUETA[MODELO_ETIQUETA_PADRAO]
    m = dict(base)
    m['codigo'] = codigo if codigo in MODELOS_ETIQUETA else MODELO_ETIQUETA_PADRAO
    m['por_folha'] = m['colunas'] * m['linhas']
    m['papel_largura'], m['papel_altura'] = PAPEL_CARTA_MM
    m['margem_topo'] = ETIQUETA_MARGEM_TOPO_MM
    m['margem_esquerda'] = ETIQUETA_MARGEM_ESQUERDA_MM
    m['passo_horizontal'] = ETIQUETA_PASSO_HORIZONTAL_MM
    # O vao e o que sobra do passo depois da etiqueta. Entre LINHAS nao ha vao:
    # o passo vertical e a propria altura.
    m['vao_coluna'] = round(ETIQUETA_PASSO_HORIZONTAL_MM - m['largura'], 2)
    return m


# Compatibilidade: o padrao continua sendo 80 por folha.
ETIQUETAS_POR_FOLHA = 80


def _ensure_etiquetas_schema():
    from sqlalchemy import text as sa_text
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS etiquetas_impressas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                chave_atleta VARCHAR(120) NOT NULL,
                impresso_em DATETIME NOT NULL,
                impresso_por VARCHAR(120),
                CONSTRAINT _etiqueta_uc UNIQUE (cidade, ano, mes, chave_atleta)
            )
        """))


def _etiquetas_do_mes(cidade, ano, mes):
    """Medalhistas congelados do mes + marca de quem ja teve etiqueta."""
    from sqlalchemy import text as sa_text
    from app.blueprints.avisos import medalhistas_do_mes
    _ensure_etiquetas_schema()

    impressos = {r[0] for r in db.session.execute(sa_text(
        "SELECT chave_atleta FROM etiquetas_impressas "
        "WHERE cidade = :c AND ano = :a AND mes = :m"),
        {'c': cidade, 'a': ano, 'm': mes}).fetchall()}

    itens = []
    for m in medalhistas_do_mes(cidade, ano, mes):
        modalidades = []
        for mod in m.get('modalidades') or []:
            modalidades.append('Ciclismo' if mod == 'ciclismo' else 'Corrida/Caminhada')
        itens.append({
            'chave': m['chave'],
            'nome': m['nome'],
            'km': m.get('km') or 0,
            'modalidades': sorted(set(modalidades)),
            'impresso': m['chave'] in impressos,
        })
    itens.sort(key=lambda i: (i['nome'] or '').lower())
    return itens


@admin_bp.route('/indice')
@login_required
@viewer_required   # 20/09/2026 (item 73): o indice e' o MANUAL das telas — todo
                   # perfil le o que cada uma faz; clicar e' que depende do nivel.
def indice_telas():
    """Todas as telas do site, por assunto — e as que ninguém alcança."""
    from app import indice as _indice
    arvore = _indice.construir(current_app)
    return render_template(
        'admin/indice.html', arvore=arvore,
        total=sum(len(c['telas']) for c in arvore),
        orfas=sum(c['orfas'] for c in arvore),
        dominios_velhos=sum(c['dominios_velhos'] for c in arvore))


# ===================== PAINEL DOS MEDALHISTAS (P-52) =====================
#
# POR QUE ESTA TELA EXISTE
# A lista de medalhistas vivia espremida dentro da central de mensagens: sem
# dizer de que mes era, sem como atualizar, sem como marcar quem ja pegou e sem
# como reenviar para quem faltou. Quem nao pegou a medalha ha tres meses
# simplesmente nao era mais encontravel.
#
# Aqui o admin tem o mes inteiro na frente: quem bateu a meta, quem ja recebeu
# o aviso, quem ja retirou, e quantas medalhas cada um tem acumuladas sem pegar.
#
# A CHAVE DE ENVIO E A MESMA DO CICLO (`ciclo:medalha-AAAA-MM-<atleta>`).
# Se fosse outra, a mesma pessoa receberia duas vezes: uma pelo botao daqui e
# outra pela agenda automatica.

_PREFIXO_MEDALHA = 'ciclo:medalha-'


def _dedupe_medalha(ano, mes, chave):
    return '%s%04d-%02d-%s' % (_PREFIXO_MEDALHA, ano, mes, chave)


def _medalhistas_do_painel(cidade, ano, mes):
    """Cada medalhista do mes com telefone, aviso, retirada e acumulado."""
    from sqlalchemy import text as sa_text
    from app import filamensagens
    from app.blueprints.avisos import medalhistas_do_mes, ensure_schema as _av_schema
    from app.mensagens import _telefones_dos_medalhistas
    _av_schema()

    pessoas = medalhistas_do_mes(cidade, ano, mes)
    chaves = [p['chave'] for p in pessoas]
    telefones = _telefones_dos_medalhistas(chaves)

    retirados = {r[0]: r[1] for r in db.session.execute(sa_text(
        "SELECT chave_atleta, retirado_em FROM medalhas_retiradas "
        "WHERE cidade=:c AND ano=:a AND mes=:m"),
        {'c': cidade, 'a': ano, 'm': mes}).fetchall()}

    # Quantas medalhas a pessoa tem sem retirar, contando TODOS os meses. Duas
    # ou mais acumuladas e o sinal de que o aviso nao esta funcionando para ela.
    acumulado = {}
    for chave, quantos in db.session.execute(sa_text("""
            SELECT m.chave_atleta, COUNT(DISTINCT m.ano || '-' || m.mes)
            FROM medalhistas_mes m
            LEFT JOIN medalhas_retiradas r
              ON r.cidade = m.cidade AND r.ano = m.ano AND r.mes = m.mes
             AND r.chave_atleta = m.chave_atleta
            WHERE m.cidade = :c AND r.id IS NULL
            GROUP BY m.chave_atleta"""), {'c': cidade}).fetchall():
        acumulado[str(chave)] = int(quantos or 0)

    _prefixo = '%s%04d-%02d-' % (_PREFIXO_MEDALHA, ano, mes)
    despachadas = filamensagens.situacao_por_dedupe(cidade, prefixo=_prefixo) or {}
    # QUANTAS VEZES já saiu para cada um (pedido do dono, 13/08). Com duas
    # telas mandando a mesma cobrança, era fácil avisar a mesma pessoa duas
    # vezes sem perceber.
    quantas_vezes = filamensagens.contagem_por_dedupe(cidade, prefixo=_prefixo) or {}

    itens = []
    for p in pessoas:
        chave = str(p['chave'])
        envio = despachadas.get(_dedupe_medalha(ano, mes, chave)) or {}
        itens.append({
            'chave': chave,
            'nome': p['nome'],
            'km': p.get('km') or 0,
            'modalidades': sorted({'Ciclismo' if m == 'ciclismo'
                                   else 'Corrida/Caminhada'
                                   for m in (p.get('modalidades') or [])}),
            'telefone': telefones.get(chave),
            'retirada': retirados.get(chave),
            'aviso': envio.get('situacao'),
            'aviso_em': envio.get('quando'),
            'envios': quantas_vezes.get(_dedupe_medalha(ano, mes, chave), 0),
            'acumuladas': acumulado.get(chave, 0),
        })
    itens.sort(key=lambda i: (i['nome'] or '').lower())
    return itens


def _logo_do_patrocinador(cidade, ano, mes):
    """(blob, mime, nome) da logo do patrocinador daquele mês, ou None.

    Sai do PERÍODO (regra 10): a logo de julho é a de quem bancou julho, nunca
    a de quem banca agora. Procura primeiro na linha do mês e, se ela não tiver
    arte própria, na ficha do catálogo de onde o mês foi copiado.
    """
    try:
        from app.models import PatrocinadorMensal, PatrocinadorCatalogo
        mensais = PatrocinadorMensal.query.filter_by(
            cidade=cidade, ano=ano, mes=mes).order_by(
            PatrocinadorMensal.ordem.asc()).all()
        for sp in mensais:
            if sp.patrocinador_logo_blob:
                return (sp.patrocinador_logo_blob,
                        sp.patrocinador_logo_mime or 'image/png',
                        sp.patrocinador_logo_filename or 'patrocinador.png')
            if sp.catalogo_id:
                ficha = PatrocinadorCatalogo.query.get(sp.catalogo_id)
                if ficha and ficha.patrocinador_logo_blob:
                    return (ficha.patrocinador_logo_blob,
                            ficha.patrocinador_logo_mime or 'image/png',
                            ficha.patrocinador_logo_filename or 'patrocinador.png')
    except Exception as exc:
        logger.warning('Não consegui pegar a logo de %s %s/%s: %s',
                       cidade, mes, ano, exc)
    return None


def _mes_anterior_de(quando):
    """(ano, mes) do mês que fechou. É o período das medalhas e do agradecimento."""
    return (quando.year - 1, 12) if quando.month == 1 else (quando.year, quando.month - 1)


def _referencia_do_mes(ano, mes, agora):
    """A data que faz `mensagens_do_ciclo` falar do período (ano, mes).

    O SELETOR ESCOLHE O MÊS DO CONTEÚDO, não "faça de conta que hoje é".
    `mensagens_do_ciclo` monta as medalhas do mês ANTERIOR ao de referência —
    é assim que em agosto aparecem as medalhas de julho, que é o certo. Então a
    referência é um dia do mês SEGUINTE ao escolhido.

    Virou função em 14/08/2026 porque a Central calculava isto na tela e o botão
    "Salvar e enviar" **não** calculava: ele chamava `mensagens_do_ciclo(cidade)`
    sem período nenhum. Escolher julho na tela e mandar agosto é o tipo de
    desencontro que ninguém percebe até a mensagem errada chegar a 80 pessoas.
    """
    if (ano, mes) == _mes_anterior_de(agora):
        return agora
    ano_ref, mes_ref = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return datetime(ano_ref, mes_ref, min(agora.day, 28), agora.hour, agora.minute)


def _enfileirar_medalha(cidade, ano, mes, item, por, forcar=False):
    """Poe UMA cobranca de medalha na fila. Devolve (ok, motivo).

    `forcar` REPETE de proposito, furando a trava de duplicata — e o mesmo
    mecanismo do botao "Enviar novamente" da Central
    (`filamensagens.enfileirar(..., forcar=True)`).

    POR QUE PRECISOU (13/08/2026)
    O botao de enviar da tela /admin/medalhistas "nao fazia nada": ele chamava
    esta funcao SEM `forcar`, batia na trava de duplicata e devolvia "ja estava
    na fila" — enquanto o mesmo botao na Central funcionava, porque de la ia
    com `forcar=sim`. Duas telas para a mesma acao com comportamento diferente
    e o pior tipo de defeito: nao da erro, so nao acontece.

    As DUAS telas usam esta funcao e o MESMO `dedupe` (`_dedupe_medalha`), que
    e o que faz o registro de envio ser um so — avisar por uma tela aparece na
    outra.
    """
    from app import filamensagens
    from app.mensagens import cobranca_de_medalha
    if not item.get('telefone'):
        return False, '%s está sem telefone' % item['nome']

    # A LOGO DO PATROCINADOR VAI COMO IMAGEM, O AVISO COMO LEGENDA (P-67).
    # Quem banca a medalha aparece na mensagem de quem vai buscá-la — é o único
    # momento em que ele encontra o atleta um a um. Some-se à regra 11-A: a
    # logo é imagem, sempre. Sem logo cadastrada, segue como texto puro; a
    # mensagem nunca deixa de sair por falta de arte.
    imagem = _logo_do_patrocinador(cidade, ano, mes)

    for pessoa in cobranca_de_medalha(cidade, ano, mes, so_pendentes=False):
        if str(pessoa['chave_atleta']) != item['chave']:
            continue
        texto = pessoa['texto']
        # QUEM JÁ RECEBEU E RECEBE DE NOVO lê, logo no começo e em destaque,
        # que o aviso é de um mês que já passou (pedido do dono, 01/10/2026) —
        # sem isso a pessoa acha que ganhou outra medalha, do mês atual.
        if forcar and (item.get('envios') or item.get('aviso') == 'enviada'):
            from app.mensagens import aviso_de_mes_anterior, rodape_de_reincidencia
            texto = aviso_de_mes_anterior(ano, mes) + texto + rodape_de_reincidencia()
        _id, ja = filamensagens.enfileirar(
            cidade, 'medalha', item['telefone'], texto,
            imagem=imagem,
            dedupe=_dedupe_medalha(ano, mes, item['chave']), criado_por=por,
            forcar=forcar)
        if _id is None:
            return False, '%s não entrou na fila' % item['nome']
        return True, ('%s já estava na fila' % item['nome']) if ja else ''
    return False, '%s não está na lista deste mês' % item['nome']


@admin_bp.route('/medalhistas', methods=['GET', 'POST'])
@login_required
@admin_required
def medalhistas():
    """Quem bateu a meta, mês a mês: avisar, marcar retirada, acompanhar."""
    from sqlalchemy import text as sa_text
    from app.blueprints.ranking import MESES_PT
    from app.blueprints.avisos import refazer_medalhistas
    from app.mensagens import mes_da_medalha

    cidade = current_user.cidade_ativa
    agora = datetime.now()
    padrao_ano, padrao_mes = mes_da_medalha(agora)
    ano = request.values.get('ano', type=int) or padrao_ano
    mes = request.values.get('mes', type=int) or padrao_mes

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()
        quem = current_user.email

        if acao == 'atualizar':
            quantos = refazer_medalhistas(cidade, ano, mes)
            flash('Lista refeita com os km de agora: %d medalhista(s). '
                  'A tela de etiquetas usa a mesma lista e já está atualizada '
                  'também.' % quantos, 'success')

        elif acao in ('retirou', 'nao_retirou'):
            chave = (request.form.get('chave') or '').strip()
            if acao == 'retirou':
                db.session.execute(sa_text(
                    "INSERT OR IGNORE INTO medalhas_retiradas "
                    "(cidade, ano, mes, chave_atleta, retirado_em, marcado_por) "
                    "VALUES (:c, :a, :m, :k, :em, :por)"),
                    {'c': cidade, 'a': ano, 'm': mes, 'k': chave,
                     'em': agora, 'por': quem})
            else:
                db.session.execute(sa_text(
                    "DELETE FROM medalhas_retiradas WHERE cidade=:c AND ano=:a "
                    "AND mes=:m AND chave_atleta=:k"),
                    {'c': cidade, 'a': ano, 'm': mes, 'k': chave})
            db.session.commit()

        elif acao == 'salvar_local_mes':
            # ENDEREÇO DE ENTREGA DESTE MÊS (13/08). Nasce copiado da ficha do
            # patrocinador e pode ser mudado AQUI sem tocar no cadastro dele —
            # poucas vezes a entrega é em outro lugar, e quando é, não faz
            # sentido corromper o endereço do balcão por causa de um mês.
            from app.blueprints.avisos import ensure_schema as _av_schema2
            _av_schema2()
            db.session.execute(sa_text("""
                INSERT OR REPLACE INTO local_retirada
                (cidade, ano, mes, estabelecimento, endereco, maps_url,
                 horario_funcionamento, atualizado_em)
                VALUES (:c, :a, :m, :e, :end, :maps, :h, :em)
            """), {
                'c': cidade, 'a': ano, 'm': mes,
                'e': (request.form.get('estabelecimento') or '').strip()[:160],
                'end': (request.form.get('endereco') or '').strip()[:300],
                'maps': (request.form.get('maps_url') or '').strip()[:500],
                'h': (request.form.get('horario') or '').strip()[:160],
                'em': datetime.now(),
            })
            db.session.commit()
            flash('Endereço de entrega de %s/%s salvo. A ficha do patrocinador '
                  'não foi alterada.' % (MESES_PT.get(mes, mes), ano), 'success')

        elif acao in ('enviar', 'enviar_faltantes', 'cobrar_todos'):
            itens = _medalhistas_do_painel(cidade, ano, mes)
            # Enviar UMA pessoa é sempre decisão explícita do admin — ele
            # clicou naquela linha. Então repete mesmo que já tenha ido, igual
            # ao "Enviar novamente" da Central. "Avisar quem falta" continua
            # sem forçar: lá o risco é mandar de novo para 14 pessoas sem querer.
            # "COBRAR QUEM NÃO RETIROU" (01/10/2026, pedido do dono): todo mundo
            # que ainda não foi marcado como "retirou", já avisado ou não — antes
            # era preciso clicar de um em um. A tela confirma com a contagem.
            forcar = acao in ('enviar', 'cobrar_todos')
            if acao == 'enviar':
                chave = (request.form.get('chave') or '').strip()
                alvos = [i for i in itens if i['chave'] == chave]
            elif acao == 'cobrar_todos':
                alvos = [i for i in itens if not i['retirada']]
            else:
                # Quem falta = ainda nao retirou E ainda nao recebeu aviso.
                # Quem ja recebeu nao entra: repetir aviso de graca e o que
                # ensina a pessoa a ignorar o aviso.
                alvos = [i for i in itens
                         if not i['retirada'] and not i['aviso']]
            ok, avisos = 0, []
            for item in alvos:
                deu, motivo = _enfileirar_medalha(cidade, ano, mes, item, quem,
                                                  forcar=forcar)
                if deu:
                    ok += 1
                if motivo:
                    avisos.append(motivo)
            if ok:
                # ENTREGA NA HORA quando a máquina está de pé (item 10). Sem
                # isto o admin esperava até 15 minutos pelo ciclo do Dell e
                # achava que o sistema tinha travado — medido: pedido 20:22,
                # chegada 20:36. Falhando, a mensagem só continua na fila.
                _tentar_entregar_agora(cidade, ok)
            if avisos:
                flash('Não deu para: ' + '; '.join(avisos[:8]), 'warning')
            if not ok and not avisos:
                flash('Ninguém para avisar: todo mundo já recebeu ou já retirou.',
                      'info')

        return voltar('admin.medalhistas', ano=ano, mes=mes)

    itens = _medalhistas_do_painel(cidade, ano, mes)
    # ONDE A MEDALHA É RETIRADA, NESTA TELA (pedido do dono, 13/08). É aqui que
    # ele decide mandar o aviso, então é aqui que ele precisa ver o que vai
    # dentro dele — sem abrir outra página para conferir. Vem da mesma fonte da
    # mensagem: local do mês, ou o endereço da ficha do patrocinador do mês.
    try:
        from app.blueprints.avisos import _local_retirada
        local_mes = _local_retirada(cidade, ano, mes) or {}
    except Exception as exc:
        logger.warning('Medalhistas: não consegui apurar o local: %s', exc)
        local_mes = {}

    return render_template(
        'admin/medalhistas.html', cidade=cidade, ano=ano, mes=mes,
        local_mes=local_mes,
        itens=itens, meses=MESES_PT,
        anos=list(range(agora.year + 1, agora.year - 5, -1)),
        sem_telefone=[i for i in itens if not i['telefone']],
        a_avisar=[i for i in itens if not i['retirada'] and not i['aviso']],
        retiradas=[i for i in itens if i['retirada']])


@admin_bp.route('/etiquetas', methods=['GET', 'POST'])
@login_required
@admin_required
def etiquetas():
    """Escolhe quem entra na folha de etiquetas do mês."""
    from sqlalchemy import text as sa_text
    from app.blueprints.ranking import MESES_PT
    cidade = current_user.cidade_ativa
    agora = datetime.now()
    ano = request.values.get('ano', type=int) or agora.year
    mes = request.values.get('mes', type=int) or agora.month

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()
        _ensure_etiquetas_schema()
        if acao == 'desmarcar':
            chave = (request.form.get('chave') or '').strip()
            db.session.execute(sa_text(
                "DELETE FROM etiquetas_impressas WHERE cidade=:c AND ano=:a "
                "AND mes=:m AND chave_atleta=:k"),
                {'c': cidade, 'a': ano, 'm': mes, 'k': chave})
            db.session.commit()
            flash('Etiqueta liberada para reimpressão.', 'info')

        # REFAZER A LISTA DE MEDALHISTAS.
        # A lista sai de uma foto congelada (`medalhistas_mes`), tirada na
        # primeira vez que alguem abre o mes. Se os km mudarem depois — foi o
        # caso de uma atleta em 06/08, que saiu de 277 para 404 km depois da
        # importacao pelo perfil e passou a bater a meta —, a foto continua a
        # mesma e a pessoa nao aparece para imprimir etiqueta. O ranking ja
        # mostrava; a etiqueta, nao.
        # Refazer e um clique, e nao e automatico de proposito: a foto existe
        # para o mes fechado nao mudar sozinho (regra 9). Quem decide refazer
        # e o admin, que sabe o que corrigiu.
        if acao == 'atualizar':
            # MESMA funcao que o painel de medalhistas usa. Refazer aqui
            # atualiza la tambem, porque as duas telas leem `medalhistas_mes`.
            # Antes este codigo estava escrito so aqui e o dono precisava
            # lembrar de vir a esta tela para consertar a outra.
            from app.blueprints.avisos import refazer_medalhistas
            quantos = refazer_medalhistas(cidade, ano, mes)
            flash('Lista refeita com os km de agora: %d medalhista(s). '
                  'Quem já teve etiqueta impressa continua marcado, e o painel '
                  'de medalhistas já está atualizado também.' % quantos,
                  'success')
        return voltar('admin.etiquetas', ano=ano, mes=mes)

    itens = _etiquetas_do_mes(cidade, ano, mes)
    anos = list(range(agora.year + 1, agora.year - 5, -1))
    modelo = _modelo_etiqueta(request.values.get('modelo'))
    return render_template('admin/etiquetas.html',
                           cidade=cidade, ano=ano, mes=mes, itens=itens,
                           meses=MESES_PT, anos=anos,
                           modelo=modelo, modelos=MODELOS_ETIQUETA,
                           por_folha=modelo['por_folha'])


@admin_bp.route('/etiquetas/folha', methods=['POST'])
@login_required
@admin_required
def etiquetas_folha():
    """Monta a folha pronta para imprimir e marca quem entrou nela."""
    from sqlalchemy import text as sa_text
    from app.blueprints.ranking import MESES_PT
    cidade = current_user.cidade_ativa
    ano = request.form.get('ano', type=int) or datetime.now().year
    mes = request.form.get('mes', type=int) or datetime.now().month
    modelo = _modelo_etiqueta(request.form.get('modelo'))
    por_folha = modelo['por_folha']
    pular = max(0, min(por_folha - 1, request.form.get('pular', type=int) or 0))
    marcar = (request.form.get('marcar') or '') == 'sim'
    escolhidos = set(request.form.getlist('incluir'))

    itens = [i for i in _etiquetas_do_mes(cidade, ano, mes) if i['chave'] in escolhidos]
    if not itens:
        flash('Escolha pelo menos um medalhista.', 'warning')
        return voltar('admin.etiquetas', ano=ano, mes=mes)

    if marcar:
        _ensure_etiquetas_schema()
        agora = datetime.now()
        for item in itens:
            db.session.execute(sa_text(
                "INSERT OR REPLACE INTO etiquetas_impressas "
                "(cidade, ano, mes, chave_atleta, impresso_em, impresso_por) "
                "VALUES (:c, :a, :m, :k, :em, :por)"),
                {'c': cidade, 'a': ano, 'm': mes, 'k': item['chave'],
                 'em': agora, 'por': current_user.email})
        db.session.commit()

    # Posicoes vazias no comeco = a folha que voltou da gaveta pela metade.
    celulas = [None] * pular + itens
    while len(celulas) % por_folha:
        celulas.append(None)
    folhas = [celulas[i:i + por_folha]
              for i in range(0, len(celulas), por_folha)]

    return render_template('admin/etiquetas_folha.html',
                           cidade=cidade, ano=ano, mes=mes, folhas=folhas,
                           mes_nome=MESES_PT.get(mes, mes), total=len(itens),
                           modelo=modelo)


@admin_bp.route('/etiquetas/pdf', methods=['POST'])
@login_required
@admin_required
def etiquetas_pdf():
    """A MESMA folha, em PDF com as medidas travadas (P-140, 20/09/2026).

    Por que existe: imprimindo o HTML, o diálogo do navegador pode encolher a
    folha ("ajustar à página"), trocar o papel para A4 (2,7% menor que Carta) ou
    pôr margem própria — e qualquer um dos três empurra o nome para fora do
    adesivo, que foi o que aconteceu com as etiquetas de agosto. No PDF a página
    é Carta de verdade e cada etiqueta tem coordenada absoluta.

    A folha sai com uma régua de 100 mm no rodapé: quem imprime confere com uma
    régua de verdade e descobre na hora se a impressora está escalando.
    """
    from app import etiquetaspdf
    from app.blueprints.ranking import MESES_PT
    cidade = current_user.cidade_ativa
    ano = request.form.get('ano', type=int) or datetime.now().year
    mes = request.form.get('mes', type=int) or datetime.now().month
    modelo = _modelo_etiqueta(request.form.get('modelo'))
    por_folha = modelo['por_folha']
    pular = max(0, min(por_folha - 1, request.form.get('pular', type=int) or 0))
    com_regua = (request.form.get('regua') or 'sim') == 'sim'
    escolhidos = set(request.form.getlist('incluir'))

    itens = [i for i in _etiquetas_do_mes(cidade, ano, mes)
             if not escolhidos or i['chave'] in escolhidos]
    if not itens:
        flash('Escolha pelo menos um medalhista.', 'warning')
        return voltar('admin.etiquetas', ano=ano, mes=mes)

    mes_nome = MESES_PT.get(mes, mes)
    celulas = [None] * pular + itens
    # SO A PRIMEIRA FOLHA por PDF, como a tela: quem tem mais de 80 medalhistas
    # gera de novo pulando as posicoes usadas — e assim ninguem imprime 3
    # folhas achando que era 1.
    celulas = celulas[:por_folha]
    linhas = []
    for item in celulas:
        if not item:
            linhas.append({'nome': '', 'detalhe': ''})
            continue
        linhas.append({
            'nome': item.get('nome') or '',
            'detalhe': '%s/%s - %.1f km - %s' % (
                mes_nome, ano, item.get('km') or 0,
                ' + '.join(item.get('modalidades') or [])),
        })

    pdf = etiquetaspdf.gerar(
        linhas, modelo, com_regua=com_regua,
        titulo='MoveMentes - %s - %s/%s - modelo Pimaco %s - imprimir em '
               'Tamanho real (100%%), papel Carta'
               % (cidade, mes_nome, ano, modelo['codigo']))
    from flask import Response
    nome_arquivo = 'etiquetas_%s_%02d_%s.pdf' % (
        cidade.lower().replace(' ', '-'), mes, ano)
    return Response(pdf, mimetype='application/pdf', headers={
        'Content-Disposition': 'attachment; filename="%s"' % nome_arquivo,
    })


# ===================== MENSAGENS DO CICLO MENSAL =====================

@admin_bp.route('/mensagens')
@admin_bp.route('/centraldemensagem')
@login_required
@admin_required
def mensagens_ciclo():
    """Endereços antigos das mensagens. Tudo mora na Central agora.

    /admin/mensagens mostrava so o agradecimento e a abertura; a Central
    mostra isso e o resto (avisos, anticheat, fila). Ter as duas telas era o
    jeito mais rapido de o admin olhar a errada e achar que faltava coisa.
    """
    return redirect(url_for('admin.central_mensagens'))


@admin_bp.route('/reviews/<int:flag_id>/mensagem')
@login_required
@admin_required
def mensagem_revisao(flag_id):
    """Texto pronto para falar com o atleta sobre uma flag.

    Tom leve, culpa no aparelho, nunca acusatório: o objetivo é entender o que
    houve. Devolve JSON para a tela mostrar sem recarregar.
    """
    from app.mensagens import mensagem_para_atleta
    flag = FlagAnticheat.query.get_or_404(flag_id)
    atividade = flag.atividade
    if atividade and atividade.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)
    atleta = flag.atleta
    nome = (atleta.nome_atual if atleta else None) or (
        atividade.nome_usuario if atividade else 'atleta')
    telefone = atleta.telefone_normalizado if atleta else None
    texto = mensagem_para_atleta(flag, nome=nome, indice=flag.id)
    return jsonify(
        texto=texto,
        nome=nome,
        telefone=telefone,
        whatsapp_url=(f'https://wa.me/{telefone}' if telefone else None),
    )


# ===================== PAINEL DA AUTOMACAO (automayk) =====================
# O dominio outro-dominio.exemplo.com aponta para esta mesma aplicacao, atras do
# Cloudflare Zero Trust. E a porta de entrada dos admins de cidade para o que
# e da automacao — sem NUNCA expor o painel do WAHA, que da acesso a todas as
# sessoes e a todas as conversas de todas as cidades.

@admin_bp.route('/automayk')
@login_required
@supremo_required  # 19/09/2026 (item 73): horarios e diagnostico da maquina
def painel_automayk():
    """Tudo da automação da MINHA cidade, num lugar só."""
    from app import backupauto, filamensagens, publicador, secretstore, whatsapp as wa
    from app import fechamento as fech
    from app.models import MonthlyBackup

    cidade = current_user.cidade_ativa
    agora = datetime.now()

    url, chave = _waha_config(cidade)
    saude_strava = secretstore.session_health(cidade) or {}

    abertos = []
    try:
        ano, mes = fech._mes_anterior(agora.year, agora.month)
        for _ in range(6):
            if (fech._ja_passou_a_carencia(ano, mes, agora)
                    and not fech.esta_fechado(cidade, ano, mes)
                    and MonthlyBackup.query.filter_by(cidade=cidade, ano=ano, mes=mes).first()):
                abertos.append((ano, mes))
            ano, mes = fech._mes_anterior(ano, mes)
    except Exception as exc:
        logger.debug('painel automayk/fechamento: %s', exc)

    return render_template(
        'admin/automayk.html',
        cidade=cidade,
        waha_configurado=bool(url and chave),
        sessao=wa.nome_sessao(cidade),
        responsavel=wa.numero_responsavel(cidade),
        pedido=wa.pedido_atual(cidade),
        token_ok=secretstore.info(secretstore.AUTOMATION_TOKEN, scope=cidade).get('configurado'),
        strava_ok=saude_strava.get('ok'),
        strava_configurado=bool(secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)),
        meses_abertos=abertos,
        backups=backupauto.situacao(),
        # Horarios e fila passaram a ser mostrados AQUI: antes eram dois
        # cliques a mais, e o admin nao tinha como olhar uma tela so e saber
        # se a cidade ia publicar.
        agenda=publicador.agenda(cidade),
        problemas=publicador.conflitos(cidade),
        resumo_fila=filamensagens.resumo(cidade),
    )


@admin_bp.route('/reinstalacao')
@login_required
@supremo_required
def reinstalacao():
    """Kit de reinstalação: o que instalar, em que ordem, o que reiniciar."""
    import platform
    import sys as _sys
    ambiente = {
        'python': _sys.version.split()[0],
        'plataforma': platform.platform(),
        'raiz': str(_PROJECT_ROOT),
        'backups': str(_BACKUP_DIR),
    }
    requisitos = []
    caminho_req = _PROJECT_ROOT / 'requirements.txt'
    try:
        if caminho_req.is_file():
            with open(caminho_req, 'r', encoding='utf-8', errors='replace') as fh:
                requisitos = [l.strip() for l in fh if l.strip() and not l.startswith('#')]
    except OSError as exc:
        logger.warning('Nao consegui ler requirements.txt: %s', exc)
    return render_template('admin/reinstalacao.html',
                           ambiente=ambiente, requisitos=requisitos)


# ===================== PAINEL DE POSTAGEM =====================
# Onde a cidade define QUANDO publica, O QUE sai em cada horario e DE QUAL
# WhatsApp. Antes isso estava preso no Agendador do Windows e no systemd —
# mudar 10:30 para 11:00 exigia mexer em duas maquinas.

@admin_bp.route('/postagem', methods=['GET', 'POST'])
@login_required
@admin_required
def postagem():
    """Horários de publicação e quem publica (principal, suplente, socorro)."""
    from app import publicador
    from app.models import PostagemAgenda
    cidade = current_user.cidade_ativa

    if request.method == 'POST':
        acao = (request.form.get('acao') or '').strip()

        if acao == 'salvar_agenda':
            for item in publicador.agenda(cidade):
                pref = 'h{}_'.format(item.id)
                item.ativo = request.form.get(pref + 'ativo') == 'sim'
                item.envia_ranking = request.form.get(pref + 'ranking') == 'sim'
                item.envia_patrocinadores = request.form.get(pref + 'patro') == 'sim'
                item.envia_parabens = request.form.get(pref + 'parabens') == 'sim'
                item.envia_avisos = request.form.get(pref + 'avisos') == 'sim'
                item.envia_promocoes = request.form.get(pref + 'promo') == 'sim'
                novo_horario = (request.form.get(pref + 'horario') or '').strip()
                if re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', novo_horario):
                    item.horario = novo_horario
            db.session.commit()
            flash('Horários salvos.', 'success')
            return voltar('admin.postagem')

        if acao == 'novo_horario':
            horario = (request.form.get('horario') or '').strip()
            if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', horario):
                flash('Informe o horário como HH:MM, por exemplo 10:30.', 'warning')
                return voltar('admin.postagem')
            if PostagemAgenda.query.filter_by(cidade=cidade, horario=horario).first():
                flash('Já existe um horário {}.'.format(horario), 'warning')
                return voltar('admin.postagem')
            db.session.add(PostagemAgenda(
                cidade=cidade, horario=horario, ativo=True,
                envia_ranking=True, envia_patrocinadores=True,
                envia_parabens=True, envia_avisos=True, envia_promocoes=False,
                criado_em=datetime.now()))
            db.session.commit()
            flash('Horário {} criado.'.format(horario), 'success')
            return voltar('admin.postagem')

        if acao == 'apagar_horario':
            alvo = PostagemAgenda.query.get(request.form.get('id', type=int) or 0)
            if alvo and alvo.cidade == cidade:
                db.session.delete(alvo)
                db.session.commit()
                flash('Horário removido.', 'info')
            return voltar('admin.postagem')

        if acao == 'salvar_envio':
            # Quem publica. A ordem e o que decide principal x suplente.
            alvos = Usuario.query.filter_by(cidade=cidade).all()
            for u in alvos:
                if not current_user.pode_gerir(u) and u.id != current_user.id:
                    continue
                u.envia_ranking = request.form.get('u{}_ranking'.format(u.id)) == 'sim'
                u.envia_avisos = request.form.get('u{}_avisos'.format(u.id)) == 'sim'
                u.ordem_envio = request.form.get('u{}_ordem'.format(u.id), type=int) or 0
            db.session.commit()
            flash('Configuração de envio salva.', 'success')
            return voltar('admin.postagem')

        return voltar('admin.postagem')

    usuarios = Usuario.query.filter_by(cidade=cidade).order_by(
        Usuario.ordem_envio.asc(), Usuario.nome.asc()).all()
    return render_template(
        'admin/postagem.html',
        cidade=cidade,
        itens=publicador.agenda(cidade),
        usuarios=usuarios,
        candidatos_ranking=publicador.candidatos_envio(cidade, 'ranking'),
        candidatos_avisos=publicador.candidatos_envio(cidade, 'avisos'),
        problemas=publicador.conflitos(cidade),
        agora=publicador.horario_de_agora(cidade),
    )


_TROCA_REFERENCIA = (
    "UPDATE sqlite_master SET sql = replace(sql, "
    '\'"processed_files_old_uc"\', \'"processed_files"\') '
    "WHERE type='table' AND name='activities'"
)


@admin_bp.route('/manutencao/reparar-referencia', methods=['POST'])
@login_required
@supremo_required
def reparar_referencia_orfa():
    """Conserta a chave estrangeira órfã da tabela de atividades.

    O QUE ACONTECEU: uma migração antiga renomeou `processed_files` para um
    nome temporário. O SQLite, sozinho, reescreveu a referência dentro de
    `activities` para apontar para esse nome temporário — e o DROP seguinte
    apagou o alvo. Ficou uma referência para uma tabela que não existe.

    Ficou inofensivo por semanas porque o SQLite não confere chave
    estrangeira por padrão. Só apareceu quando a checagem foi ligada, e aí
    todo salvamento de atividade quebrou.

    O conserto reescreve APENAS o texto do schema (não copia a tabela, que
    tem milhares de linhas) e só roda com backup verificado antes.
    """
    origem = _PROJECT_ROOT / _SQLITE_DB_RELATIVE_PATH
    con = None
    try:
        con = sqlite3.connect('file:{}?mode=ro'.format(origem), uri=True, timeout=30)
        atual = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='activities'"
        ).fetchone()
    finally:
        if con is not None:
            con.close()

    if not atual or 'processed_files_old_uc' not in (atual[0] or ''):
        flash('Nada a consertar: a referência já está correta.', 'info')
        return redirect(url_for('admin.comandos'))

    ok, detalhe = backup_antes_de_apagar('reparo-referencia-activities')
    if not ok:
        flash('Reparo cancelado: não consegui um backup confiável ({}).'.format(detalhe), 'error')
        return redirect(url_for('admin.comandos'))

    con = None
    try:
        con = sqlite3.connect(str(origem), timeout=60)
        versao = con.execute('PRAGMA schema_version').fetchone()[0]
        con.execute('PRAGMA writable_schema=ON')
        con.execute(_TROCA_REFERENCIA)
        con.execute('PRAGMA schema_version={}'.format(versao + 1))
        con.execute('PRAGMA writable_schema=OFF')
        con.commit()
        estado = con.execute('PRAGMA integrity_check').fetchone()[0]
        if str(estado).lower() != 'ok':
            raise RuntimeError('integrity_check apos o reparo: {}'.format(estado))
    except Exception as exc:
        logger.error('Falha ao reparar referencia orfa: %s', exc, exc_info=True)
        flash('O reparo falhou: {}. O backup está em {} — nada foi perdido.'.format(exc, detalhe), 'error')
        return redirect(url_for('admin.comandos'))
    finally:
        if con is not None:
            con.close()

    logger.warning('Referencia orfa de activities reparada por %s (backup: %s)',
                   current_user.email, detalhe)
    flash('Referência consertada. Backup salvo em ' + detalhe +
          '. Reinicie a aplicação para o banco reabrir com o schema novo.', 'success')
    return redirect(url_for('admin.comandos'))


# ===================== CENTRAL DE MENSAGENS =====================
# UMA tela para tudo que o sistema fala com as pessoas, em tres trilhas:
#
#   Acontece sozinho  - todo mes, no mesmo lugar do calendario
#   Voce programa     - so existe se alguem criar (aviso, promocao)
#   Esperando voce    - assunto sensivel, so sai se o admin apertar
#
# A pergunta que separa as trilhas: "se eu nao fizer nada, isso sai?"
# Antes isso estava espalhado em /avisos e /admin/mensagens, e nao dava para
# olhar uma tela e saber o que era automatico.

@admin_bp.route('/central', methods=['GET'])
@login_required
@admin_required
def central_mensagens():
    """Tudo que o sistema manda: o que sai sozinho, o que você programa e o que espera você."""
    from app import agendamensagens, filamensagens, publicador, textos
    from app.mensagens import mensagens_do_ciclo
    from app.blueprints.ranking import MESES_PT
    from app.blueprints.avisos import listar_avisos, aviso_vencido, ensure_schema as _av_schema

    cidade = current_user.cidade_ativa
    agora = datetime.now()

    # PERÍODO ESCOLHIDO NA TELA (13/08). Sem isto tudo saía de `datetime.now()`
    # e o admin não tinha como ver nem reenviar o que era de um mês fechado —
    # ele clicava "enviar" torcendo para ser o mês certo. Vale para as três
    # trilhas: ciclo, medalhistas e modelos.
    # Padrão = o mês que FECHOU (o anterior), porque é dele que são as medalhas
    # e o agradecimento que a tela mostra ao abrir.
    _ano_pad, _mes_pad = _mes_anterior_de(agora)
    ano = request.args.get('ano', type=int) or _ano_pad
    mes = request.args.get('mes', type=int) or _mes_pad
    if not 1 <= mes <= 12:
        mes = _mes_pad
    # O seletor escolhe o MÊS DO CONTEÚDO — a conta mora em `_referencia_do_mes`,
    # porque o botão "Salvar e enviar" precisa fazer exatamente a mesma.
    referencia = _referencia_do_mes(ano, mes, agora)

    # --- trilha 1: acontece sozinho ---
    try:
        ciclo = mensagens_do_ciclo(cidade, hoje=referencia)
    except Exception as exc:
        logger.error('Central: falha ao montar o ciclo do mes: %s', exc, exc_info=True)
        ciclo = []
    agenda = publicador.agenda(cidade)

    # --- trilha 2: voce programa ---
    try:
        _av_schema()
        avisos = listar_avisos(cidade)
    except Exception as exc:
        logger.warning('Central: falha ao listar avisos: %s', exc)
        avisos = []
    for aviso in avisos:
        try:
            aviso['vencido'] = aviso_vencido(aviso, agora)
        except Exception:
            aviso['vencido'] = False

    # --- trilha 3: esperando voce ---
    esperando = _itens_esperando(cidade)

    # Situacao de cada mensagem do ciclo: ja esta na fila? ja saiu? quando?
    # Sem isto o botao "enviar agora" ficava aceso para sempre e nao dava para
    # saber se a mensagem tinha saido.
    situacoes = filamensagens.situacao_por_dedupe(cidade, prefixo='ciclo:')
    # QUANTAS VEZES JA SAIU (01/10/2026, pedido do dono): o numero fica em
    # cada mensagem, para nao mandar a mesma coisa duas, tres vezes e
    # incomodar a pessoa. Conta so o que saiu de fato (`enviada`).
    vezes = filamensagens.contagem_por_dedupe(cidade, prefixo='ciclo:')
    hoje_sai = {i['chave'] for i in agendamensagens.planejar(cidade, hoje=agora)}
    for item in ciclo:
        item['situacao'] = situacoes.get('ciclo:' + item['chave'])
        item['vezes'] = vezes.get('ciclo:' + item['chave'], 0)
        item['sai_hoje_sozinha'] = item['chave'] in hoje_sai
        item['janela'] = agendamensagens.CALENDARIO.get(item['tipo'])

    # Uma consulta só, usada nas duas listas abaixo.
    _painel_do_mes = _medalhistas_do_painel(cidade, ano, mes)

    return render_template(
        'admin/central_mensagens.html',
        cidade=cidade, agora=agora,
        ano=ano, mes=mes, meses=MESES_PT,
        anos=list(range(agora.year + 1, agora.year - 5, -1)),
        periodo_atual=((ano, mes) == (agora.year, agora.month)),
        # A seção de medalhas da Central é um ATALHO da tela de medalhistas,
        # alimentada pela MESMA fonte (`_medalhistas_do_painel`) — não uma
        # segunda lista. Aqui só quem FALTA: quem já retirou ou já foi avisado
        # se resolve na tela própria, que tem os controles todos.
        medalhistas_faltantes=[i for i in _painel_do_mes
                               if not i['retirada'] and not i['aviso']],
        # Os já avisados vão junto, recolhidos: sem eles a CONTAGEM de envios
        # ficava invisível justamente quando ela importa — quando todo mundo já
        # recebeu e a dúvida é "mandei quantas vezes para quem?".
        medalhistas_avisados=[i for i in _painel_do_mes
                              if i['retirada'] or i['aviso']],
        ciclo=ciclo, agenda=agenda, avisos=avisos, esperando=esperando,
        fila=filamensagens.pendentes(cidade=cidade, limite=60),
        # As que desistiram aparecem AQUI, com o motivo. O alerta manda "abra a
        # Central e veja quais são" — antes ele mandava para uma tela vazia.
        fila_falhadas=filamensagens.falhadas(cidade),
        resumo_fila=filamensagens.resumo(cidade),
        textos_padrao=textos.PADROES,
        textos_editados={k: textos.editado(cidade, k) for k in textos.PADROES},
        textos_corpo={k: textos.corpo(cidade, k) for k in textos.PADROES},
        # O que impede o aviso das medalhas de sair, se for o caso, para a tela
        # mostrar isso JUNTO do botão em vez de só depois do clique.
        bloqueio_medalhas=_bloqueio_medalhas(cidade, ano, mes),
    )


def _bloqueio_medalhas(cidade, ano, mes):
    """`mensagens.bloqueio_medalhas_no_grupo` sem deixar a Central cair."""
    from app import mensagens as _msg
    try:
        return _msg.bloqueio_medalhas_no_grupo(cidade, ano, mes)
    except Exception as exc:
        logger.warning('Central: não consegui apurar o bloqueio das medalhas: %s',
                       exc)
        return None


def _itens_esperando(cidade):
    """O que precisa de uma decisão sua antes de sair.

    Hoje: revisões de anti-cheat sem contato feito. A promoção de
    patrocinador entra aqui quando o cadastro por link ficar pronto.
    """
    itens = []
    try:
        flags = (FlagAnticheat.query
                 .join(Activity, FlagAnticheat.atividade_id == Activity.id)
                 .filter(Activity.cidade == cidade,
                         FlagAnticheat.status.in_(('aberta', 'em_revisao')))
                 .order_by(FlagAnticheat.criada_em.desc())
                 .limit(30).all())
    except Exception as exc:
        logger.warning('Central: falha ao listar flags: %s', exc)
        flags = []
    for flag in flags:
        atleta = flag.atleta
        itens.append({
            'tipo': 'anticheat',
            'titulo': f'Suspeita em {(atleta.nome_atual if atleta else "atleta")}',
            'detalhe': (flag.descricao or flag.tipo_flag or '')[:160],
            'quando': flag.criada_em,
            'flag_id': flag.id,
            'tem_telefone': bool(atleta and atleta.telefone_normalizado),
            'link': url_for('admin.reviews'),
        })
    return itens


@admin_bp.route('/central/texto', methods=['POST'])
@login_required
@admin_required
def central_texto():
    """Salva, restaura ou envia um dos textos do mês."""
    from app import filamensagens, textos
    from app.mensagens import mensagens_do_ciclo

    cidade = current_user.cidade_ativa
    chave = (request.form.get('chave') or '').strip()
    acao = (request.form.get('acao') or '').strip()
    if chave not in textos.PADROES:
        flash('Texto desconhecido.', 'warning')
        return voltar('admin.central_mensagens')

    if acao == 'restaurar':
        textos.restaurar(cidade, chave)
        flash('Texto voltou ao padrão do sistema.', 'info')
        return voltar('admin.central_mensagens')

    if acao in ('salvar', 'salvar_enviar'):
        textos.salvar(cidade, chave, request.form.get('corpo'),
                      por=current_user.email)
        if acao == 'salvar':
            flash('Texto salvo. Ele sai no próximo horário de publicação.', 'success')
            return voltar('admin.central_mensagens')

    # salvar_enviar: monta com os dados do MÊS ESCOLHIDO e poe na fila agora.
    agora = datetime.now()
    ano = request.form.get('ano', type=int) or _mes_anterior_de(agora)[0]
    mes = request.form.get('mes', type=int) or _mes_anterior_de(agora)[1]
    if not 1 <= mes <= 12:
        ano, mes = _mes_anterior_de(agora)

    prefixo = _MODELO_PARA_CICLO.get(chave)
    enviados = repetidas = 0
    if prefixo:
        for item in mensagens_do_ciclo(cidade, hoje=_referencia_do_mes(ano, mes, agora)):
            if not item['chave'].startswith(prefixo):
                continue
            destino = item.get('telefone')
            if not destino and item.get('destino') == 'grupo':
                destino = 'grupo'
            if not destino:
                continue
            _id, ja_saiu = filamensagens.enfileirar(
                cidade, item['tipo'], destino, item['texto'],
                # As marcacoes (@) do aviso de medalhas viajam com a mensagem:
                # o texto ja tem o "@numero", e isto diz a quem ele se refere.
                mencionar=item.get('mencionar') or None,
                dedupe=f"ciclo:{item['chave']}", criado_por=current_user.email)
            if _id is None:
                continue
            if ja_saiu:
                repetidas += 1
            else:
                enviados += 1

    if enviados:
        _tentar_entregar_agora(cidade, enviados)
        if repetidas:
            flash('Outras %d já tinham saído e não vão sair de novo.'
                  % repetidas, 'info')
    elif repetidas:
        flash('Texto salvo. As %d mensagem(ns) deste modelo já tinham saído — '
              'não vão sair de novo. Use "mandar de novo" na lista de cima se '
              'for mesmo para repetir.' % repetidas, 'info')
    else:
        flash('Texto salvo, mas ' + _porque_nao_saiu(cidade, chave, ano, mes),
              'warning')
    return voltar('admin.central_mensagens')


# DE QUAL ITEM DO CICLO CADA MODELO EDITÁVEL FALA.
#
# Antes isto era adivinhado com `chave.split('_')[0]` + `startswith`, e a
# adivinhação errava em TRÊS dos oito modelos: `fechamento_mes` procurava
# "fechamento" (o item se chama `encerramento-…`), `cobranca_medalha` procurava
# "cobranca" (o item é `medalha-…`) e `medalhas_no_grupo` procurava "medalhas"
# (o item não existia — P-70 pela metade). Nos três, o botão "Salvar e enviar"
# salvava o texto e não enviava nada, dizendo apenas "não havia mensagem para
# enviar agora". Casar por prefixo calculado funciona por sorte e falha calado.
#
# `medalha-` NÃO pega `medalhas-…`: depois de "medalha" vem "s", não "-".
_MODELO_PARA_CICLO = {
    'agradecimento':            'agradecimento-',
    'fechamento_mes':           'encerramento-',
    'abertura_com_patrocinador': 'abertura-',
    'abertura_sem_patrocinador': 'abertura-',
    'patrocinio_garantido':     'patrocinio-',
    'patrocinio_encontrado':    'patrocinio-',
    'medalhas_no_grupo':        'medalhas-',
    'cobranca_medalha':         'medalha-',
}


def _porque_nao_saiu(cidade, chave, ano, mes):
    """Por que este modelo não gerou mensagem — em português, com onde resolver.

    "Não havia mensagem para enviar agora" é verdade e não serve para nada:
    quem lê fica sem saber se o sistema falhou, se já tinha enviado ou se falta
    um dado. Regra 16-I: dizer o que aconteceu E o que fazer.
    """
    from app import mensagens as _msg

    if chave not in _MODELO_PARA_CICLO:
        return ('este texto ainda não tem botão de disparo: ele é usado quando '
                'você fala com o patrocinador, não no ciclo do mês.')

    if chave == 'medalhas_no_grupo':
        try:
            bloqueio = _msg.bloqueio_medalhas_no_grupo(cidade, ano, mes)
        except Exception as exc:
            logger.warning('Não consegui apurar o bloqueio das medalhas: %s', exc)
            bloqueio = None
        if bloqueio:
            return '%s Resolva em "%s" e clique de novo.' % (
                bloqueio['texto'], bloqueio['botao'])

    return ('não há nada deste tipo para o período %02d/%d. Confira o mês '
            'escolhido no alto da tela.' % (mes, ano))


@admin_bp.route('/central/fila/<int:mensagem_id>/enviar', methods=['POST'])
@login_required
@admin_required
def central_fila_enviar(mensagem_id):
    """Tenta entregar ESTA mensagem agora, sem esperar o ciclo de 15 minutos.

    POR QUE (pedido do dono, 14/08): em fase de teste, esperar o ciclo para
    saber se um botão funcionou torna cada tentativa um exercício de paciência.
    Aqui ele aperta e a tela responde na hora — inclusive quando não deu, e
    dizendo por quê.
    """
    from app import filamensagens as _fila
    cidade = current_user.cidade_ativa
    try:
        entregues, motivo = _fila.entregar_agora(cidade, limite=60,
                                                 apenas_id=mensagem_id)
    except Exception as exc:
        logger.warning('Envio imediato de %s falhou: %s', mensagem_id, exc)
        entregues, motivo = 0, 'não deu para entregar agora'
    if entregues:
        flash('Enviada agora.', 'success')
    else:
        flash('Não saiu: %s. Ela continua na fila e sai no próximo ciclo.'
              % motivo, 'warning')
    return voltar('admin.central_mensagens')


def _tentar_entregar_agora(cidade, quantas):
    """Tenta entregar a fila JÁ e conta o resultado na tela.

    POR QUE VIROU FUNÇÃO (14/08/2026)
    A entrega imediata (item 10) estava ligada só na tela de medalhistas. Quem
    apertava "enviar" na Central via "na fila, esperando a automação ligar" com
    a máquina LIGADA na frente dele — que é exatamente a queixa que o item 10
    existia para resolver. Duas telas para a mesma ação com comportamento
    diferente é o pior tipo de defeito: não dá erro, só não acontece.
    """
    from app import filamensagens as _fila
    try:
        entregues, motivo = _fila.entregar_agora(cidade, limite=max(quantas, 5))
    except Exception as exc:
        logger.warning('Entrega imediata falhou em %s: %s', cidade, exc)
        entregues, motivo = 0, 'não deu para entregar agora'
    if entregues < quantas:
        # O que nao saiu agora (maquina dormindo, WhatsApp parado) esperava o
        # ciclo de 15 minutos do Dell. Com o atalho, sai no proximo minuto.
        _adiantar_execucao(cidade, 'painel', 'entregar mensagens')
    if entregues:
        flash('%d mensagem(ns) enviada(s) agora.' % entregues, 'success')
        if entregues < quantas:
            flash('As outras %d ficaram na fila e saem no próximo ciclo '
                  '(até 15 min).' % (quantas - entregues), 'info')
    else:
        flash('%d mensagem(ns) na fila — %s. Saem no próximo ciclo '
              '(até 15 min).' % (quantas, motivo), 'info')
    return entregues


@admin_bp.route('/central/enviar', methods=['POST'])
@login_required
@admin_required
def central_enviar():
    """Coloca UMA mensagem do ciclo na fila (botão "enviar agora")."""
    from app import filamensagens
    from app.mensagens import mensagens_do_ciclo

    cidade = current_user.cidade_ativa
    alvo = (request.form.get('chave_item') or '').strip()
    # "Mandar de novo" e uma decisao consciente: sem isto, a trava que impede
    # a rotina automatica de repetir tambem impediria o admin de reenviar.
    forcar = (request.form.get('forcar') or '') == 'sim'
    # O MÊS DA MENSAGEM VEM NA CHAVE (01/10/2026). A Central mostra o ciclo do
    # mês escolhido no seletor, mas este botão procurava só no ciclo de HOJE:
    # em agosto, "Mandar de novo" o aviso de medalhas dava "Mensagem não
    # encontrada". Mesmo defeito que o "Salvar e enviar" teve em 14/08.
    # Procura em hoje, no mês da chave e no anterior a ele (a abertura fala do
    # mês da referência, não do conteúdo).
    candidatos = list(mensagens_do_ciclo(cidade))
    _m = re.search(r'-(\d{4})-(\d{2})', alvo)
    if _m:
        _a, _mm = int(_m.group(1)), int(_m.group(2))
        _pa, _pm = (_a - 1, 12) if _mm == 1 else (_a, _mm - 1)
        for _ano, _mes in ((_a, _mm), (_pa, _pm)):
            try:
                candidatos += mensagens_do_ciclo(
                    cidade, hoje=_referencia_do_mes(_ano, _mes, datetime.now()))
            except Exception as exc:
                logger.warning('Central: nao montei o ciclo de %s/%s: %s', _mes, _ano, exc)
    for item in candidatos:
        if item['chave'] != alvo:
            continue
        destino = item.get('telefone') or ('grupo' if item.get('destino') == 'grupo' else None)
        if not destino:
            flash(f"“{item['titulo']}” não tem para onde ir: falta o telefone "
                  "no cadastro de quem vai receber.", 'warning')
            return voltar('admin.central_mensagens')
        texto = item['texto']
        if forcar and item['tipo'] in ('medalhas_grupo', 'medalha') and item.get('periodo'):
            # Repetição do aviso de medalhas: começa dizendo, em destaque, que
            # é de um mês anterior e não do vigente (pedido do dono, 01/10/2026).
            from app.mensagens import aviso_de_mes_anterior, rodape_de_reincidencia
            _a, _m = (int(x) for x in item['periodo'].split('-'))
            texto = aviso_de_mes_anterior(_a, _m) + texto + rodape_de_reincidencia()
        _id, repetida = filamensagens.enfileirar(
            cidade, item['tipo'], destino, texto,
            mencionar=item.get('mencionar') or None,
            dedupe=f"ciclo:{item['chave']}", criado_por=current_user.email,
            forcar=forcar)
        if _id is None:
            flash('O telefone cadastrado não é um número válido de WhatsApp. '
                  'Corrija o cadastro e tente de novo.', 'warning')
        elif repetida:
            flash('Essa mensagem já saiu (ou já está na fila) — não vai sair '
                  'duas vezes. Use “mandar de novo” se for mesmo para repetir.',
                  'info')
        else:
            _tentar_entregar_agora(cidade, 1)
        return voltar('admin.central_mensagens')
    flash('Mensagem não encontrada.', 'warning')
    return voltar('admin.central_mensagens')


@admin_bp.route('/central/fila/<int:mensagem_id>/cancelar', methods=['POST'])
@login_required
@admin_required
def central_fila_cancelar(mensagem_id):
    from app import filamensagens
    filamensagens.marcar(mensagem_id, filamensagens.CANCELADA,
                         detalhe=f'cancelado por {current_user.email}',
                         cidade=current_user.cidade_ativa)
    flash('Mensagem cancelada — não vai sair.', 'info')
    return voltar('admin.central_mensagens')


@admin_bp.route('/central/fila/<int:mensagem_id>/retomar', methods=['POST'])
@login_required
@admin_required
def central_fila_retomar(mensagem_id):
    """Devolve para a fila uma mensagem que tinha desistido.

    É o botão que faltava depois de um conserto: a mensagem morreu por um
    defeito que já foi corrigido, e sem isto a única saída era refazer tudo.
    """
    from app import filamensagens
    cidade = current_user.cidade_ativa
    if not filamensagens.retomar(mensagem_id, cidade=cidade):
        flash('Essa mensagem não está mais parada — nada a refazer.', 'info')
        return voltar('admin.central_mensagens')
    _tentar_entregar_agora(cidade, 1)
    return voltar('admin.central_mensagens')
