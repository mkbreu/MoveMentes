# -*- coding: utf-8 -*-
"""Backups automaticos — os que ninguem precisa lembrar de fazer.

DUAS TRILHAS, com ritmos diferentes porque perdem coisas diferentes:

  BANCO DE DADOS — 1x por DIA, a partir das 6h.
      E o que muda todo dia: atletas, km, patrocinadores, avisos, usuarios.
      Perder um dia de trabalho ja doi; perder dez seria inaceitavel.

  CODIGO (aplicacao inteira) — 1x por SEMANA.
      O Git guarda o historico, mas backup existe para o dia em que a rede
      nao esta disponivel — por isso ele mora aqui tambem.

RETENCAO, nos dois: "avo-pai-filho" (`_rotacionar_gfs`) — todos os dos ultimos
7 dias, 1 por semana nas ultimas 4, 1 por mes nos ultimos 6. Cobre meio ano com
17 arquivos em vez de 180.

ONDE: /volume1/web/Backups (fora da raiz web — backup dentro do site e
backup publicado).

QUEM RODA: a propria aplicacao, numa thread. O NAS nao tem cron confiavel do
usuario. Como o uWSGI roda 2 workers, cada tentativa passa por uma trava em
arquivo — sem ela os dois fariam o mesmo backup ao mesmo tempo.
"""
import json
import logging
import os
import sqlite3
import time
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BACKUP_DIR = _PROJECT_ROOT.parent / 'Backups'
_ESTADO = _BACKUP_DIR / 'agendamento.json'
_TRAVA = _PROJECT_ROOT / 'temp_extract' / 'backupauto.lock'

# RETENCAO "AVO-PAI-FILHO" (26/08/2026, pedido do dono). Antes: banco 3x por
# mes guardando os 9 mais novos, codigo 1x por mes guardando os 12 mais novos.
# Dois problemas: o banco muda TODO DIA (3x por mes podia custar 10 dias de
# trabalho) e "os N mais novos" e' uma escada estreita -- um problema notado
# duas semanas depois nao tinha de onde voltar. Agora: copia diaria, e a
# rotacao guarda muito mais tempo com menos arquivos (ver _rotacionar_gfs).
#
# Conta de espaco, com os tamanhos de hoje: banco 52 MB x 17 copias = ~880 MB,
# codigo 6 MB x 17 = ~100 MB. O volume tem 5,9 TB livres.
GFS_DIARIOS = 7          # todos os dos ultimos 7 dias
GFS_SEMANAIS = 4         # + 1 por semana, nas ultimas 4 semanas
GFS_MENSAIS = 6          # + 1 por mes, nos ultimos 6 meses

# Antes de sair da frente do ranking: o NAS liga 05:13 e a publicacao e' 05:30.
# Backup do banco le 52 MB num aparelho de 2 nucleos -- nao pode disputar a
# maquina com a unica coisa que tem hora marcada.
HORA_MINIMA_BACKUP = 6

DIAS_CONFIG = (1, 11, 21)   # mantido so para nao quebrar `situacao()`
MAX_CONFIG = 9
MAX_CODIGO = 12
_TRAVA_VALIDA_SEGUNDOS = 3600


def _carregar_estado():
    try:
        with open(_ESTADO, 'r', encoding='utf-8') as fh:
            dados = json.load(fh)
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar_estado(dados):
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _ESTADO.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(dados, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, _ESTADO)
    except OSError as exc:
        logger.warning('Nao consegui gravar o agendamento de backup: %s', exc)


def _pegar_trava():
    """Trava entre os workers do uWSGI. Devolve True se ficou com ela.

    A trava expira sozinha: se um worker morrer no meio, no ciclo seguinte
    outro assume em vez de o backup parar para sempre.
    """
    try:
        _TRAVA.parent.mkdir(parents=True, exist_ok=True)
        if _TRAVA.exists():
            idade = time.time() - _TRAVA.stat().st_mtime
            if idade < _TRAVA_VALIDA_SEGUNDOS:
                return False
            _TRAVA.unlink()
        fd = os.open(str(_TRAVA), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode('ascii'))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        logger.warning('Trava de backup indisponivel (%s); pulando a rodada', exc)
        return False


def _soltar_trava():
    try:
        _TRAVA.unlink()
    except OSError:
        pass


def _rotacionar_gfs(prefixo, diarios=7, semanais=4, mensais=6):
    """Retencao "avo-pai-filho": o padrao da area, e o que o dono pediu.

    POR QUE NAO "OS N MAIS NOVOS" (26/08/2026)
    Guardar so os N mais novos e' uma escada estreita: com backup diario e N=9,
    o mais antigo tem 9 dias -- um problema descoberto duas semanas depois nao
    tem de onde voltar. E guardar 30 diarios so para ter 1 mes de alcance
    desperdica espaco em copias quase identicas.

    A regra aqui cobre muito mais tempo com menos arquivos:
      - TODOS os dos ultimos `diarios` dias   (erro recente, volta fino)
      - 1 por semana das ultimas `semanais`   (erro de algumas semanas)
      - 1 por mes dos ultimos `mensais`       (o historico longo)
    Em cada semana/mes sobrevive o MAIS NOVO. O resto sai.

    Backup nunca e' apagado por engano de calendario: se algo der errado ao ler
    a data de um arquivo, ele e' mantido (a duvida joga a favor de guardar).
    """
    try:
        arquivos = sorted(
            (p for p in _BACKUP_DIR.glob(f'{prefixo}_*')
             if p.is_file() and p.suffix != '.parcial'),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except OSError:
        return 0

    agora = datetime.now()
    manter, vistos_semana, vistos_mes = set(), set(), set()
    for p in arquivos:
        try:
            quando = datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:
            manter.add(p)          # na duvida, guarda
            continue
        idade_dias = (agora - quando).days
        if idade_dias < diarios:
            manter.add(p)
            continue
        semana = quando.isocalendar()[:2]        # (ano, numero da semana)
        if len(vistos_semana) < semanais and semana not in vistos_semana:
            vistos_semana.add(semana)
            manter.add(p)
            continue
        mes = (quando.year, quando.month)
        if len(vistos_mes) < mensais and mes not in vistos_mes:
            vistos_mes.add(mes)
            manter.add(p)

    removidos = 0
    for antigo in arquivos:
        if antigo in manter:
            continue
        try:
            antigo.unlink()
            removidos += 1
            logger.info('Backup antigo removido na rotacao: %s', antigo.name)
        except OSError as exc:
            logger.warning('Nao consegui remover %s: %s', antigo, exc)
    return removidos


def _limpar_sidecars(caminho):
    """Apaga os -wal/-shm que o SQLite deixa ao lado da copia.

    Sem isto a pasta de backups enche de arquivos soltos e fica dificil
    saber, batendo o olho, quantas copias boas existem de verdade.
    """
    for sufixo in ('-wal', '-shm'):
        vizinho = Path(str(caminho) + sufixo)
        try:
            if vizinho.exists():
                vizinho.unlink()
        except OSError:
            pass


def _snapshot_banco(destino):
    """Copia consistente do SQLite (API .backup, le o WAL corretamente)."""
    origem = _PROJECT_ROOT / 'instance' / 'movimente_se.db'
    if not origem.is_file():
        raise RuntimeError('banco de dados nao encontrado')
    src = sqlite3.connect(str(origem), timeout=30)
    dst = sqlite3.connect(str(destino))
    try:
        try:
            src.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            pass
        src.backup(dst)
        # A copia sai em modo DELETE: um arquivo unico, que se copia para um
        # pendrive sem esquecer pedaco.
        try:
            dst.execute('PRAGMA journal_mode=DELETE')
        except sqlite3.Error:
            pass
    finally:
        dst.close()
        src.close()
    _limpar_sidecars(destino)
    # Backup que ninguem abriu nao e backup.
    con = sqlite3.connect(f'file:{destino}?mode=ro', uri=True, timeout=30)
    try:
        estado = con.execute('PRAGMA integrity_check').fetchone()[0]
        if str(estado).lower() != 'ok':
            raise RuntimeError(f'copia ilegivel: integrity_check={estado}')
        con.execute('SELECT COUNT(*) FROM activities').fetchone()
    finally:
        con.close()
    _limpar_sidecars(destino)


def backup_configuracao():
    """Backup do banco (o que muda todo dia). Devolve o caminho gerado.

    O arquivo nasce como `.parcial` DENTRO da pasta de backups e so recebe o
    nome final depois de passar na verificacao. Assim ninguem confunde uma
    copia pela metade com um backup bom -- e o rename e no mesmo sistema de
    arquivos (mover entre volumes falharia).
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    destino = _BACKUP_DIR / f'config_{stamp}.db'
    tmp = _BACKUP_DIR / f'config_{stamp}.db.parcial'
    try:
        _snapshot_banco(tmp)
        os.replace(str(tmp), str(destino))
    finally:
        _limpar_sidecars(tmp)
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    _rotacionar_gfs('config', GFS_DIARIOS, GFS_SEMANAIS, GFS_MENSAIS)
    logger.warning('Backup automatico de configuracao: %s', destino.name)
    return destino


_CODIGO_EXCLUIR_DIRS = {
    '.git', 'venv', 'temp_extract', 'nginx_tmp', '__pycache__',
    '.pytest_cache', '.mypy_cache', '.ruff_cache', 'uploads', 'logs',
    'instance',
}
_CODIGO_EXCLUIR_SUFIXOS = {'.pyc', '.pyo', '.tmp', '.log'}


def backup_codigo():
    """Backup da aplicacao (sem banco, sem uploads, sem venv)."""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    destino = _BACKUP_DIR / f'codigo_{stamp}.zip'
    total = 0
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for raiz, dirs, arquivos in os.walk(_PROJECT_ROOT):
            raiz_path = Path(raiz)
            dirs[:] = [d for d in dirs if d not in _CODIGO_EXCLUIR_DIRS]
            for nome in arquivos:
                caminho = raiz_path / nome
                if caminho.suffix.lower() in _CODIGO_EXCLUIR_SUFIXOS:
                    continue
                try:
                    zf.write(caminho, arcname=str(caminho.relative_to(_PROJECT_ROOT)))
                    total += 1
                except OSError as exc:
                    logger.debug('Arquivo pulado no backup de codigo (%s): %s', nome, exc)
    _rotacionar_gfs('codigo', GFS_DIARIOS, GFS_SEMANAIS, GFS_MENSAIS)
    logger.warning('Backup automatico de codigo: %s (%s arquivos)', destino.name, total)
    return destino


def rodar_backups_agendados(agora=None, forcar=None):
    """Roda o que estiver vencido. Chamada varias vezes ao dia sem problema.

    `forcar` aceita 'config' ou 'codigo' para disparar na hora (botao do
    painel), ignorando o calendario mas respeitando a trava e a rotacao.
    """
    agora = agora or datetime.now()
    if not _pegar_trava():
        return {}
    feitos = {}
    try:
        estado = _carregar_estado()

        # ---- banco: TODO DIA (e' o que muda todo dia) ----
        # A chave e' a data: rodou hoje, nao roda de novo, por mais vezes que a
        # rotina passe. Antes das 6h so sai se alguem pedir pelo botao.
        chave_cfg = agora.strftime('%Y-%m-%d')
        na_hora = agora.hour >= HORA_MINIMA_BACKUP
        if forcar == 'config' or (na_hora and estado.get('ultima_config') != chave_cfg):
            try:
                caminho = backup_configuracao()
                feitos['config'] = str(caminho)
                if chave_cfg:
                    estado['ultima_config'] = chave_cfg
                estado['ultima_config_em'] = agora.isoformat(timespec='seconds')
            except Exception as exc:
                logger.error('Backup de configuracao falhou: %s', exc, exc_info=True)

        # ---- codigo: 1x por SEMANA ----
        # Mudou de mensal para semanal: o codigo passou a mudar toda semana, e
        # um backup mensal deixaria semanas de trabalho sem copia local. (O Git
        # cobre o historico, mas backup existe para o dia em que a rede nao
        # esta disponivel -- por isso ele mora aqui tambem.)
        ano_semana = '%04d-S%02d' % agora.isocalendar()[:2]
        if forcar == 'codigo' or (na_hora and estado.get('ultimo_codigo') != ano_semana):
            try:
                caminho = backup_codigo()
                feitos['codigo'] = str(caminho)
                estado['ultimo_codigo'] = ano_semana
                estado['ultimo_codigo_em'] = agora.isoformat(timespec='seconds')
            except Exception as exc:
                logger.error('Backup de codigo falhou: %s', exc, exc_info=True)

        if feitos:
            _gravar_estado(estado)
    finally:
        _soltar_trava()
    return feitos


def situacao():
    """O que a tela do painel mostra: quando rodou e o que existe hoje."""
    estado = _carregar_estado()
    def _listar(prefixo):
        try:
            arquivos = sorted(
                (p for p in _BACKUP_DIR.glob(f'{prefixo}_*')
                 if p.is_file() and p.suffix != '.parcial'),
                key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []
        return [{
            'nome': p.name,
            'tamanho_mb': round(p.stat().st_size / (1024 * 1024), 1),
            'quando': datetime.fromtimestamp(p.stat().st_mtime),
        } for p in arquivos]
    return {
        'pasta': str(_BACKUP_DIR),
        'ultima_config_em': estado.get('ultima_config_em'),
        'ultimo_codigo_em': estado.get('ultimo_codigo_em'),
        'dias_config': DIAS_CONFIG,
        'max_config': MAX_CONFIG,
        'max_codigo': MAX_CODIGO,
        'config': _listar('config'),
        'codigo': _listar('codigo'),
    }
