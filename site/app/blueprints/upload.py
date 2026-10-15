# app/blueprints/upload.py
# -*- coding: utf-8 -*-
"""
Upload de ZIP do WhatsApp -> extrai TXT -> processa atividades Strava.
Suporta upload em chunks para contornar limites do Cloudflare Tunnel.
Suporta cancelamento de job com opcao de apagar atividades inseridas.
"""
from flask import Blueprint, render_template, url_for, current_app, jsonify, request as _req, redirect
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
import json
import os
import re
import subprocess
import sys
import zipfile
import shutil
import threading
import uuid
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
from sqlalchemy import inspect, text
from app import db
from app.models import ProcessedFile, Activity, FlagAnticheat, ControleDisciplinar, EvidenciaRevisao, Config
from app.blueprints.auth import admin_required
from app.blueprints.processing import (
    StravaLimiteDeVolume,
    StravaListaVazia,
    StravaMesAnteriorAConta,
    StravaPerfilRestrito,
    process_whatsapp_txt,
    parse_strava_activity,
    resolve_strava_shortlink,
    build_strava_throttle,
    StravaAuthRequired,
    StravaRateLimited,
    ScrapeCancelled,
    STRAVA_DIRECT_RE,
    STRAVA_SHORT_RE,
    STRAVA_PROFILE_RE,
    resolver_link_do_sistema,
    _sanitize_strava_activity_url,
    extract_strava_profile_activity_urls,
    fetch_athlete_month_atividades,
)
from app import secretstore
from app import jobstore
from app import energia as _energia
from app.forms import UploadForm
import logging

upload_bp = Blueprint('upload', __name__, url_prefix='/upload')
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {'zip', 'txt'}
WA_TXT_KEYWORDS = ('whatsapp', 'conversa', '_chat', 'chat with')

# ===== ESTADO DOS JOBS =====
_job_progress = {}   # job_id -> {stage, message, pct, ts, zip_filename, cidade, params}
_cancel_flags  = {}  # job_id -> {'delete': bool}
_LOCAL_THREADS = {}  # job_id -> Thread rodando NESTE worker (uWSGI tem 2 processos)
# job_id -> Popen dos trabalhos longos que este worker iniciou. So serve para
# recolher o processo quando ele termina (senao vira zumbi, e zumbi engana quem
# pergunta se o trabalho ainda esta vivo). O dono de verdade e' o arquivo em disco.
_PROCESSOS_FILHOS = {}
_PROGRESS_DIR = Path(__file__).resolve().parents[2] / 'temp_extract' / 'jobs'
_TERMINAL_STAGES = {'done', 'error', 'cancelled'}
_STALE_JOB_SECONDS = 3 * 60
# Heartbeat parado por mais que isso + nenhuma thread viva local => retomar o job.
_HEARTBEAT_STALE_SECONDS = 150

# A IMPORTACAO POR PERFIL PRECISA DE MAIS FOLGA QUE O UPLOAD COMUM (14/08/2026).
# Retomar, para ela, e RECOMECAR do primeiro mes: nao ha checkpoint. Com 150 s
# um job saudavel era declarado travado no meio de uma pausa normal do Strava e
# recomecava — 62 meses para ler, nenhum terminava.
#
# 20/08/2026 — BAIXADO DE 20 PARA 5 MINUTOS, medido ao vivo. `StravaThrottle.
# sleep()` (processing.py) manda um "estou vivo" a cada ~15s durante QUALQUER
# pausa legitima (backoff, fila compartilhada, pacing normal) — entao nenhuma
# espera normal chega perto de 5 minutos sem batimento. O que fica parado por
# minutos sem nenhum "estou vivo" e uma requisicao genuinamente travada: o
# `_HARD_REQUEST_TIMEOUT` de 75s (`processing.py`, `_hard_get`) deveria
# desistir sozinho antes disso, mas foi medido ficando preso por bem mais que
# isso (causa exata nao confirmada — hipotese principal e uma chamada de rede
# ou de parsing que segura o interpretador inteiro sem devolver o controle;
# ver ERROS.md 4.15). Ate isso ser corrigido na raiz, 5 minutos e o prazo que
# equilibra "nao confundir pausa normal com travamento" e "nao deixar o dono
# esperando 20 minutos por um resgate que a maquina ja sabe que vai precisar
# fazer".
# QUANTO TEMPO CALADO E "MORTO" (revisto em 20/08/2026, a noite).
# De manha isto caiu de 20 para 5 minutos, para "cortar o tempo de resgate por
# 4". A noite se descobriu o custo: a espera de um unico link podia passar de
# 5 minutos sem emitir batimento, e o job VIVO era declarado morto — o que
# gerou 41 retomadas num dia (ERROS.md 4.16 e 4.18). Metade da causa foi
# consertada (a espera das requisicoes agora avisa que esta viva), mas sobrou
# um trecho mudo fora da rede — a leitura da pagina, que neste NAS e lenta —
# e enquanto ele existir o prazo curto volta a acusar job saudavel.
# 12 minutos e o meio-termo honesto: acima de qualquer demora ja medida, e
# ainda bem abaixo dos 20 minutos que faziam o resgate demorar.
# RETOMAR CEDO DEMAIS NAO E DE GRACA: os dois workers do uWSGI podem acabar
# raspando o MESMO job ao mesmo tempo (o claim garante uma retomada, nao que a
# thread anterior tenha morrido), e isso dobra o ritmo no Strava (regra 4-C).
_HEARTBEAT_STALE_ATLETA = 12 * 60
# Tentativas de retomada SEM progresso novo entre elas. O contador zera sempre
# que o heartbeat avancou (campo "seq") desde a ultima retomada -- um job longo
# pode ser retomado quantas vezes for preciso, desde que esteja rendendo.
_RESUME_MAX_ATTEMPTS = 3
# Janela em que um job interrompido por restart ainda pode ser retomado.
# Ampla de proposito: um backfill de "todos os periodos" atravessa madrugadas
# e o NAS pode ficar horas desligado sem que o trabalho salvo se perca.
_RESUME_WINDOW_SECONDS = 12 * 60 * 60
# Ticker de retomada autonoma: varre os jobs em disco e reanima os travados
# mesmo sem ninguem com a pagina de upload aberta (polling do navegador).
_RESUME_TICKER_INTERVAL = 120
# Continuacao automatica: quando uma rodada bate o teto de duracao (ou o
# Strava bloqueia) TENDO progredido, o sistema agenda sozinho a proxima rodada
# do mesmo arquivo -- backlogs de qualquer tamanho terminam sem intervencao.
# Rodada sem nenhum progresso nao continua (evita rodar em circulo).
_CONTINUATION_MAX_ROUNDS = 30
_CONTINUATION_COOLDOWN_DEADLINE = 10 * 60      # pausa apos teto de duracao
_CONTINUATION_COOLDOWN_RATE_LIMIT = 45 * 60    # pausa apos bloqueio do Strava
_UPLOAD_SCHEMA_READY = False


def _ensure_upload_schema():
    global _UPLOAD_SCHEMA_READY
    if _UPLOAD_SCHEMA_READY:
        return
    insp = inspect(db.engine)
    if 'processed_files' not in set(insp.get_table_names()):
        _UPLOAD_SCHEMA_READY = True
        return

    with db.engine.begin() as conn:
        for stmt in (
            "ALTER TABLE processed_files ADD COLUMN links_scrapeados INTEGER DEFAULT 0",
            "ALTER TABLE processed_files ADD COLUMN pendencias_json TEXT",
        ):
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                msg = str(exc).lower()
                if 'duplicate column name' in msg or 'already exists' in msg:
                    continue
                raise
    _UPLOAD_SCHEMA_READY = True


def _periodos_upload(form_data, process_current_month=True):
    """Converte a opcao da tela em periodos (ano, mes) sem quebrar o campo legado."""
    modo = (form_data.get('periodo_upload') or '').strip()
    agora = datetime.now()
    atual = (agora.year, agora.month)

    if not modo:
        return {atual} if process_current_month else None
    if modo == 'mes_atual':
        return {atual}
    if modo == 'mes_atual_anterior':
        if agora.month == 1:
            anterior = (agora.year - 1, 12)
        else:
            anterior = (agora.year, agora.month - 1)
        return {atual, anterior}
    if modo == 'periodo_especifico':
        try:
            ano = int(form_data.get('periodo_ano', ''))
            mes = int(form_data.get('periodo_mes', ''))
        except (TypeError, ValueError):
            raise ValueError('Informe mes e ano validos para o periodo especifico.')
        if not 1 <= mes <= 12 or not 2000 <= ano <= 2100:
            raise ValueError('Informe mes e ano validos para o periodo especifico.')
        return {(ano, mes)}
    if modo == 'ano_especifico':
        try:
            ano = int(form_data.get('periodo_ano_completo', '') or form_data.get('periodo_ano', ''))
        except (TypeError, ValueError):
            raise ValueError('Informe um ano valido para a importacao anual.')
        if not 2000 <= ano <= 2100:
            raise ValueError('Informe um ano valido para a importacao anual.')
        if ano > agora.year:
            raise ValueError('O ano informado ainda nao comecou.')
        # Meses futuros nao existem em ranking nenhum: pedi-los so gastaria
        # requisicao ao Strava. O ano corrente para no mes PASSADO (dono,
        # 22/08/2026) -- "ano inteiro" e varredura em massa, o mes vigente
        # ainda esta aberto e se sustenta pelo link postado no grupo, nao por
        # scraping de perfil (mesma regra da Fase 2 de "todos os periodos").
        ultimo_mes = (agora.month - 1) if ano == agora.year else 12
        if ultimo_mes < 1:
            raise ValueError(
                'O ano corrente ainda nao tem nenhum mes fechado -- "ano '
                'inteiro" sempre exclui o mes vigente. Espere o mes fechar '
                'ou use "periodo especifico" para importar o mes atual.'
            )
        return {(ano, mes) for mes in range(1, ultimo_mes + 1)}
    if modo == 'todos_periodos':
        return None
    raise ValueError('Opcao de periodo de upload invalida.')


def _valor_periodo_upload(periodos):
    """Serializa o conjunto de periodos (ano, mes) num valor canonico e estavel
    para compor a chave de deduplicacao de ProcessedFile. periodos vazio/None
    (ex.: 'todos_periodos') vira o marcador fixo 'todos'."""
    if not periodos:
        return 'todos'
    return ','.join(f'{ano:04d}-{mes:02d}' for ano, mes in sorted(periodos))


def _safe_job_id(job_id):
    job_id = str(job_id or '').strip()
    if re.match(r'^[0-9a-f\-]{36}$', job_id):
        return job_id
    return None


def _progress_file_path(job_id):
    safe_job_id = _safe_job_id(job_id)
    if not safe_job_id:
        return None
    return _PROGRESS_DIR / f'{safe_job_id}.json'


def _cancel_file_path(job_id):
    safe_job_id = _safe_job_id(job_id)
    if not safe_job_id:
        return None
    return _PROGRESS_DIR / f'{safe_job_id}.cancel'


def _read_progress_file(job_id):
    path = _progress_file_path(job_id)
    if not path or not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _write_progress_file(job_id, payload):
    path = _progress_file_path(job_id)
    if not path:
        return
    try:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=True)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.debug('Falha ao gravar progresso do job %s: %s', job_id, exc)


def _delete_progress_file(job_id):
    path = _progress_file_path(job_id)
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _write_cancel_file(job_id, payload):
    path = _cancel_file_path(job_id)
    if not path:
        return
    try:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=True)
    except Exception as exc:
        logger.debug('Falha ao gravar cancelamento do job %s: %s', job_id, exc)


def _read_cancel_file(job_id):
    path = _cancel_file_path(job_id)
    if not path or not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _delete_cancel_file(job_id):
    path = _cancel_file_path(job_id)
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _extract_job_id_from_message(message):
    match = re.search(r'job_id=([0-9a-f\-]{36})', message or '')
    return match.group(1) if match else None


def _set_progress(job_id, stage, message, pct, zip_filename='', cidade=''):
    existing = _job_progress.get(job_id) or _read_progress_file(job_id) or {}
    # ULTIMA LINHA DE DEFESA (22/08/2026): depois de done/error/cancelled,
    # nenhuma escrita posterior deveria valer. Sem isto, uma thread que so
    # ficou lenta (nao morta) e foi dada como cancelada por engano continua
    # chamando _set_progress e reescreve por cima do estagio terminal -- o
    # job "ressuscita" na tela mesmo tendo sido cancelado de verdade.
    # Confirmado por varredura: nenhum lugar do codigo hoje chama
    # _set_progress duas vezes com estagio terminal para o mesmo job, entao
    # bloquear TODA escrita pos-terminal (nao so as nao-terminais) e seguro.
    if existing.get('stage') in _TERMINAL_STAGES:
        logger.info(
            'Ignorando tentativa de mudar job %s de estagio terminal %r para %r '
            '(mensagem: %s)', job_id, existing.get('stage'), stage, message)
        return
    payload = {
        'stage':        stage,
        'message':      message,
        'pct':          pct,
        'ts':           _time.time(),
        # Contador monotonico de batimentos: permite distinguir "job rendendo"
        # de "job congelado" ao decidir se uma retomada renova as tentativas.
        'seq':          int(existing.get('seq') or 0) + 1,
        'zip_filename': zip_filename or existing.get('zip_filename', ''),
        'cidade':       cidade or existing.get('cidade', ''),
        'params':       existing.get('params'),
    }
    _job_progress[job_id] = payload
    _write_progress_file(job_id, payload)


def _set_job_params(job_id, params):
    """Grava no arquivo do job os parametros que permitem retoma-lo."""
    existing = _job_progress.get(job_id) or _read_progress_file(job_id) or {}
    existing['params'] = params
    existing.setdefault('ts', _time.time())
    _job_progress[job_id] = existing
    _write_progress_file(job_id, existing)


def _resume_claim_path(job_id):
    safe_job_id = _safe_job_id(job_id)
    if not safe_job_id:
        return None
    return _PROGRESS_DIR / f'{safe_job_id}.resume'


def _claim_resume(job_id):
    """Claim atomico: garante que apenas um worker retome o job."""
    path = _resume_claim_path(job_id)
    if not path:
        return False
    try:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists() and (_time.time() - path.stat().st_mtime) > 300:
            path.unlink()
        with open(path, 'x', encoding='utf-8') as fh:
            fh.write(str(_time.time()))
        return True
    except FileExistsError:
        return False
    except Exception as exc:
        logger.debug('Falha no claim de retomada do job %s: %s', job_id, exc)
        return False


def _delete_resume_claim(job_id):
    path = _resume_claim_path(job_id)
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def _expire_job(job_id, delay=300):
    def _do():
        _time.sleep(delay)
        _job_progress.pop(job_id, None)
        _cancel_flags.pop(job_id, None)
        _LOCAL_THREADS.pop(job_id, None)
        _delete_progress_file(job_id)
        _delete_cancel_file(job_id)
        _delete_resume_claim(job_id)
    t = threading.Thread(target=_do, daemon=True)
    t.start()


def _is_cancelled(job_id):
    return job_id in _cancel_flags or _read_cancel_file(job_id) is not None


def _consume_cancel_flag(job_id):
    flag = _cancel_flags.pop(job_id, None) or _read_cancel_file(job_id) or {}
    _delete_cancel_file(job_id)
    return flag


def _job_age_seconds(job):
    try:
        return max(0, _time.time() - float(job.get('ts') or 0))
    except (TypeError, ValueError):
        return None


def _is_stale_job(job):
    if not job or job.get('stage') in _TERMINAL_STAGES:
        return False
    # PARADO DE PROPOSITO NAO E' PARADO (26/08/2026). Um trabalho congelado
    # antes do desligamento do servidor fica horas sem dar sinal -- e' o
    # combinado, nao um defeito. Sem esta linha a tela mostraria "sem resposta
    # ha 400 minutos" sobre um trabalho intacto, e o cancelamento o trataria
    # como morto.
    if (job.get('params') or {}).get('pausa_programada'):
        return False
    # Idem para quem espera vaga: esperar e' o combinado, nao um defeito.
    if job.get('stage') == _ESTAGIO_NA_FILA:
        return False
    age = _job_age_seconds(job)
    if age is None:
        return True
    # 17/08/2026 (plano attach-tidy-globe, B1) — A IMPORTACAO DE ATLETA
    # PRECISA DA MESMA FOLGA AQUI QUE JA TEM EM `_maybe_resume_job`. Sem
    # isto, a tela e o `cancel_job` chamavam de "parado" (e deixavam
    # cancelar como morto) um job saudavel que so estava numa pausa normal
    # do Strava entre 3 e 20 minutos — o mesmo tipo de falso positivo que
    # ja tinha sido corrigido para a retomada automatica, mas nao para o
    # que o admin via na tela nem para o que o cancelamento aceitava.
    kind = (job.get('params') or {}).get('kind')
    limite = _HEARTBEAT_STALE_ATLETA if kind == 'importacao_atleta' else _STALE_JOB_SECONDS
    if age <= limite:
        return False
    # MESMO CUIDADO DE `_maybe_resume_job` (20-21/08/2026): com 2 processos
    # do uWSGI, o heartbeat pode parecer parado so porque o processo dono da
    # thread nao e este que esta olhando agora. Confirma que o dono
    # realmente morreu antes de chamar o job de "parado" -- senao a tela
    # deixa cancelar (ou uma segunda importacao comecar na mesma cidade) um
    # job que so esta devagar, ainda vivo em outro worker.
    owner_pid = (job.get('params') or {}).get('owner_pid')
    if owner_pid and int(owner_pid) != os.getpid():
        # `_processo_vivo` e nao `os.kill` direto: processo zumbi engana o
        # `kill(0)` e faria um trabalho morto parecer vivo para sempre.
        if _processo_vivo(owner_pid):
            return False  # processo dono ainda de pe
    return True


def _job_atleta_em_andamento(cidade):
    """(job_id, job) da importacao de atleta desta cidade que ainda esta viva.

    POR QUE (14/08/2026, pedido do dono)
    Ate hoje o acompanhamento so existia dentro da pagina que iniciou a
    importacao: recarregar, sair da tela ou fechar o navegador e a barra sumia
    para sempre. O trabalho continuava rodando no servidor — o que sumia era a
    unica forma de ver o andamento e a unica forma de cancelar.

    O andamento ja mora em disco (`temp_extract/jobs/<id>.json`), entao basta
    procurar la: e por isso que isto sobrevive ate a fechar o navegador.

    Devolve o job MAIS RECENTE ainda em andamento, ou (None, None).
    """
    achado = (None, None)
    melhor_ts = -1.0
    try:
        if not _PROGRESS_DIR.exists():
            return achado
        for arquivo in _PROGRESS_DIR.glob('*.json'):
            try:
                with open(arquivo, 'r', encoding='utf-8') as fh:
                    job = json.load(fh)
            except Exception:
                continue
            if not isinstance(job, dict):
                continue
            if job.get('stage') in _TERMINAL_STAGES:
                continue
            if (job.get('cidade') or '') != (cidade or ''):
                continue
            # A importacao por perfil se identifica pela origem que ela grava.
            if not str(job.get('zip_filename') or '').startswith('importacao_atleta_'):
                continue
            # ORFA NAO E "EM ANDAMENTO" — E ENCERRA AQUI (14/08/2026).
            # As importacoes iniciadas antes de os parametros irem para o disco
            # nao tem como voltar: o worker que as rodava morreu e nada as
            # reanima. Mostra-las com barra animada seria mentir na tela. Em
            # 14/08 havia nove assim, sete de 9 a 14 horas antes.
            if not (job.get('params') or {}).get('kind'):
                if _is_stale_job(job):
                    _encerrar_importacao_orfa(arquivo.stem, job)
                continue
            ts = float(job.get('ts') or 0)
            if ts > melhor_ts:
                melhor_ts, achado = ts, (arquivo.stem, job)
    except Exception as exc:
        logger.debug('Nao consegui procurar importacao em andamento: %s', exc)
    return achado


# QUANTOS TRABALHOS PESADOS PODEM RODAR AO MESMO TEMPO NO SERVIDOR INTEIRO.
#
# 2 = o numero de nucleos do DS215j. Nao e' aperto: com uma ou duas cidades
# NADA muda, e nenhum pedido e' recusado -- o terceiro espera a vez e comeca
# sozinho quando abrir vaga. A alternativa (deixar entrar todo mundo) e' o
# caminho conhecido para o site cair, e um administrador de outra cidade vendo
# erro 502 desiste do sistema; vendo "o seu comeca em alguns minutos", nao.
# Quantas pendencias o trabalho guarda com o motivo escrito. O CONTADOR nunca
# e' truncado -- so esta lista. Teto porque o arquivo do job e' relido pelo
# ticker de retomada de 2 em 2 minutos, e um backfill de 64 meses pode gerar
# centenas (regra do menor custo de hardware, CLAUDE.md §3).
_TETO_PENDENCIAS = 400

# UM DE CADA VEZ (regra do dono, 28/08/2026). Dois trabalhos pesados nao
# terminam mais rapido que um: o gargalo nao e' CPU, e' o espacamento entre
# requisicoes ao Strava, que e' COMPARTILHADO (SharedPacer). Dois so faziam um
# esperar o outro -- medido nesta madrugada, os dois presos em "Outro
# processamento esta usando o Strava. Aguardando a vez", no mesmo mes, ao
# mesmo tempo. Com um so, o relatorio de cada atleta fecha antes de o proximo
# comecar, que e' o que permite tratar caso a caso.
_LIMITE_TRABALHOS_PESADOS = 1
_ESTAGIO_NA_FILA = 'aguardando'


def _trabalhos_pesados_rodando():
    """Quantos trabalhos longos estao MESMO consumindo a maquina agora.

    Nao conta o que esta pausado esperando o servidor voltar nem o que esta na
    fila: nenhum dos dois gasta CPU. E nao conta trabalho cujo processo dono ja
    morreu -- senao uma vaga ficaria presa por um fantasma.
    """
    if not _PROGRESS_DIR.exists():
        return 0
    total = 0
    for arquivo in _PROGRESS_DIR.glob('*.json'):
        try:
            with open(arquivo, 'r', encoding='utf-8') as fh:
                job = json.load(fh)
        except Exception:
            continue
        if not isinstance(job, dict):
            continue
        if job.get('stage') in _TERMINAL_STAGES or job.get('stage') == _ESTAGIO_NA_FILA:
            continue
        params = job.get('params') or {}
        if not params.get('kind') or params.get('pausa_programada'):
            continue
        pid = params.get('owner_pid')
        if params.get('processo_proprio') and pid and not _processo_vivo(pid):
            continue          # processo dono morreu: a vaga esta livre
        total += 1
    return total


def _ha_vaga_para_trabalho_pesado():
    return _trabalhos_pesados_rodando() < _LIMITE_TRABALHOS_PESADOS


_ROTULO_TRABALHO = {
    'importacao_atleta': 'Importação de atleta',
    'upload_txt':        'Upload de conversa',
    'recuperacao_links': 'Recuperação de links perdidos',
    'atualizar_perfis':  'Atualização de nomes e fotos',
}


def _quanto_faz(segundos):
    """"há 2 horas e 5 minutos" — nunca "125 min" (convenção §3 do CLAUDE.md)."""
    if segundos is None:
        return ''
    minutos = int(segundos // 60)
    if minutos < 1:
        return 'agora há pouco'
    if minutos < 60:
        return 'há %d minuto%s' % (minutos, 's' if minutos > 1 else '')
    horas, resto = divmod(minutos, 60)
    if horas < 24:
        if resto:
            return 'há %d hora%s e %d minutos' % (horas, 's' if horas > 1 else '', resto)
        return 'há %d hora%s' % (horas, 's' if horas > 1 else '')
    dias, horas = divmod(horas, 24)
    return 'há %d dia%s e %d horas' % (dias, 's' if dias > 1 else '', horas)


def _resumo_da_cota():
    """Quanto ja gastamos do Strava hoje, e quanto ainda cabe.

    E' o painel que o dono pediu (29/08/2026): antes disso, a unica forma de
    saber por que a fila tinha parado era ler o log. `nao sei` aqui devolve
    um dicionario vazio e a tela simplesmente nao mostra o painel -- melhor
    isso do que um numero inventado.
    """
    try:
        from app.ratepacer import SharedPacer, _ROTULO_JANELA
        from app import stravasinal
        pacer = SharedPacer('strava')
        tetos = pacer.tetos()
        uso = pacer.uso()
        janela, falta = pacer.estourou()
        trava = stravasinal.situacao()
    except Exception as exc:
        logger.debug('Sem resumo de cota: %s', exc)
        return {}
    linhas = []
    for chave in ('q15', 'hora', 'dia'):
        teto = int(tetos.get(chave) or 0)
        gasto = int(uso.get(chave) or 0)
        linhas.append({
            'chave': chave,
            'rotulo': _ROTULO_JANELA.get(chave, chave),
            'gasto': gasto,
            'teto': teto,
            'resta': max(0, teto - gasto),
            'pct': min(100, int(100 * gasto / teto)) if teto else 0,
        })
    return {
        'linhas': linhas,
        'travado': bool(janela),
        'janela': janela or '',
        'volta_em_min': int((falta or 0) // 60),
        'bloqueio': trava,
    }


def _id_do_trabalho(params):
    """O id do atleta deste trabalho -- dos params, senao do link.

    O `athlete_id` passou a ser gravado em 29/08/2026, mas trabalho
    criado ANTES disso nao tem o campo, e ficaria com um travessao na
    coluna ate terminar. Como o id esta no proprio `source_url`, da para
    descobrir sem guardar nada -- e ai a tela conserta tambem o passado.
    """
    alvo = (params or {}).get('athlete_id')
    if alvo:
        return str(alvo)
    achou = STRAVA_PROFILE_RE.match(((params or {}).get('source_url') or '').strip())
    return achou.group(1) if achou else None


def _nome_do_atleta(strava_id):
    """O nome de quem esta sendo importado, para a fila nao ser uma lista de
    numeros. Sem cadastro ainda (perfil novo), devolve o proprio numero -- que
    e' o que o admin colou, entao ele reconhece."""
    if not strava_id:
        return ''
    try:
        from app.models import Atleta
        a = Atleta.query.filter_by(strava_id=str(strava_id)).first()
        if a and a.nome_atual:
            return a.nome_atual
    except Exception:
        pass
    return 'perfil %s' % strava_id


def _trabalhos_em_aberto(cidade=None, todas_as_cidades=False):
    """Os trabalhos longos que ainda estao vivos, prontos para a tela.

    POR QUE (pedido do dono, 26/08/2026)
    Fechar a pagina nunca parou o trabalho -- mas so dava para reencontra-lo
    abrindo exatamente a tela de importacao da mesma cidade. Quem chegava depois
    nao tinha como saber que havia coisa rodando, nem como parar. Agora a lista
    fica na tela, com quem comecou, ha quanto tempo, em que ponto esta e o botao
    de cancelar.

    Uma varredura so: esta funcao existe para NAO haver uma segunda lendo a
    mesma pasta. `_job_atleta_em_andamento` continua com o filtro estreito dela
    (a importacao mais recente da cidade), que a tela de importacao usa para
    ligar a barra de progresso.
    """
    from app.models import Usuario

    saida = []
    if not _PROGRESS_DIR.exists():
        return saida
    agora = _time.time()
    nomes = {}
    for arquivo in _PROGRESS_DIR.glob('*.json'):
        try:
            with open(arquivo, 'r', encoding='utf-8') as fh:
                job = json.load(fh)
        except Exception:
            continue
        if not isinstance(job, dict) or job.get('stage') in _TERMINAL_STAGES:
            continue
        params = job.get('params') or {}
        if not params.get('kind'):
            continue          # sem parametros nao e' trabalho retomavel (ver acima)
        dono_cidade = job.get('cidade') or ''
        if not todas_as_cidades and dono_cidade != (cidade or ''):
            continue

        admin_id = params.get('admin_id')
        if admin_id and admin_id not in nomes:
            try:
                u = Usuario.query.get(int(admin_id))
                nomes[admin_id] = (u.nome or u.email) if u else None
            except Exception:
                nomes[admin_id] = None

        pausado = bool(params.get('pausa_programada'))
        na_fila = job.get('stage') == _ESTAGIO_NA_FILA
        try:
            pode, motivo = _pode_cancelar(job)
        except Exception:
            # Fora de um request (thread de fundo, teste) nao ha usuario logado
            # para comparar hierarquia. A lista continua servindo para MOSTRAR;
            # quem nao sabe de quem e' o acesso nao oferece o botao de parar.
            pode, motivo = False, 'Abra esta tela logado para poder parar.'
        saida.append({
            'id':        arquivo.stem,
            'tipo':      _ROTULO_TRABALHO.get(params.get('kind'), 'Trabalho longo'),
            # DE QUEM E' ESTE TRABALHO (27/08/2026, pedido do dono). Sem o nome
            # a fila e' uma lista de linhas iguais: nao da para saber quem ja
            # passou nem quem falta. `kind` e `source_url` saem daqui tambem
            # porque o clique usa esta MESMA varredura para reconhecer clique
            # duplo -- em vez de abrir uma segunda leitura da pasta.
            'kind':       params.get('kind') or '',
            'atleta':     _nome_do_atleta(_id_do_trabalho(params)),
            'athlete_id': str(params.get('athlete_id') or ''),
            'source_url': params.get('source_url') or '',
            'cidade':    dono_cidade,
            'quem':      nomes.get(admin_id) or 'não registrado',
            'desde':     _quanto_faz(agora - float(job.get('ts') or agora)),
            'pct':       int(job.get('pct') or 0),
            'mensagem':  job.get('message') or '',
            'pausado':   pausado,
            'na_fila':   na_fila,
            # DONO MORTO E' "SEM RESPOSTA", MESMO COM BATIMENTO FRESCO. O
            # batimento so prova que ALGUEM escreveu ha pouco; se o processo
            # que devia estar trabalhando ja morreu, dizer "rodando" na tela e'
            # mentira — e foi o que o teste de 26/08 mostrou.
            'parado':    (not pausado) and (not na_fila) and (
                _is_stale_job(job)
                or bool(params.get('processo_proprio') and params.get('owner_pid')
                        and not _processo_vivo(params.get('owner_pid')))),
            'pode_cancelar': pode,
            'motivo_bloqueio': motivo,
            '_ordem':    float(job.get('ts') or 0),
        })
    # ORDEM DE FILA, NAO ORDEM DE UUID (27/08/2026). Ordenar por `id` e'
    # ordenar por sorteio: quem chegou primeiro aparecia no meio. Rodando em
    # cima, depois os que aguardam pela hora em que entraram -- que e' a ordem
    # em que o ticker vai chama-los.
    saida.sort(key=lambda t: (1 if t['na_fila'] else 0, t['_ordem']))
    for t in saida:
        t.pop('_ordem', None)
    return saida


def _encerrar_importacao_orfa(job_id, job):
    """Fecha uma importacao que nao tem como ser retomada, dizendo o que houve.

    Nada se perde: cada atividade e gravada assim que e lida, entao o que ja
    entrou continua no ranking. O que falta e so ler o resto — e repetir a
    importacao e barato, porque o que ja esta salvo e pulado pela deduplicacao
    e os links curtos ficam em cache.
    """
    origem = job.get('zip_filename') or f'importacao_atleta_{job_id}'
    try:
        salvas = (Activity.query
                  .filter_by(cidade=job.get('cidade'), arquivo_origem=origem)
                  .count())
    except Exception:
        salvas = None
    recado = ('Esta importação parou quando o servidor reiniciou e não tem como '
              'continuar sozinha.')
    if salvas:
        recado += ' %d atividade(s) dela já estão salvas e continuam no ranking.' % salvas
    recado += (' Repita a importação desse atleta para ler o que faltou — o que '
               'já foi lido é pulado automaticamente.')
    _set_progress(job_id, 'error', recado, 0, origem, job.get('cidade') or '')
    _expire_job(job_id, 24 * 60 * 60)
    logger.warning('Importacao orfa encerrada: %s (%s atividade(s) salvas)',
                   job_id, salvas)


def _maybe_resume_job(job_id, job):
    """Retoma um job interrompido (reload/queda do worker) a partir do checkpoint.

    A deduplicacao por id de atividade torna a retomada idempotente: links ja
    salvos sao ignorados e o scraping continua de onde parou.
    """
    if not job or job.get('stage') in _TERMINAL_STAGES:
        return None

    # 20/08/2026 — CANCELAMENTO PEDIDO NUM JOB TRAVADO VIRAVA RETOMADA.
    #
    # Medido ao vivo: o dono clicou "Cancelar" numa importacao de atleta que
    # estava presa numa requisicao ao Strava sem responder (a thread nao
    # verifica `_is_cancelled()` enquanto esta bloqueada ali dentro — regra
    # 16-J-adjacente, o cancelamento so e visto entre uma pausa e outra). O
    # pedido de cancelar ficava gravado (arquivo `.cancel`), mas esta funcao
    # nao olhava para ele: via o heartbeat parado, achava "job morto,
    # retomar" e disparava uma thread NOVA rodando o mesmo job — o dono via
    # isso como "cliquei cancelar e ele reiniciou", que e exatamente o que
    # acontecia. Cancelamento pendente tem prioridade sobre retomada: se
    # alguem pediu para parar, a resposta certa e finalizar como cancelado,
    # nunca comecar de novo.
    if _is_cancelled(job_id):
        cancel_request = _read_cancel_file(job_id) or {}
        try:
            _finalize_stale_cancel(
                job_id, delete=bool(cancel_request.get('delete', False)), job=job,
            )
        except Exception as exc:
            logger.error('Falha ao finalizar cancelamento pendente do job %s: %s',
                         job_id, exc, exc_info=True)
        return None

    params = job.get('params') or {}

    # PAUSA PROGRAMADA: espera a maquina voltar, e nao gasta tentativa.
    #
    # Um trabalho congelado antes do desligamento (26/08/2026) fica parado de
    # proposito -- as vezes por 6 horas. O prazo normal de "morreu" o daria como
    # morto muito antes disso, e cada falso "morreu" comeria uma das 3
    # tentativas ate o sistema desistir de um trabalho que esta perfeito.
    #
    # COMO SE SABE QUE A MAQUINA JA VOLTOU: o instante do boot passou a ser mais
    # novo que o instante da pausa. Isso e' fato, nao estimativa -- comparar com
    # "ja passou das 6h?" erraria em todo reinicio fora de hora.
    if params.get('pausa_programada'):
        # DOIS GATILHOS PARA A MESMA PAUSA (27/08/2026).
        # A pausa de energia volta quando a MAQUINA reinicia; a pausa por teto
        # do Strava volta quando a JANELA vira. O mecanismo e' o mesmo (e por
        # isso e' o mesmo bloco): tira da deteccao de "parado", libera a vaga e
        # aparece como "pausado" na tela.
        if params.get('pausa_ate'):
            try:
                falta = float(params.get('pausa_ate') or 0) - _time.time()
            except (TypeError, ValueError):
                falta = 0
            # O RELOGIO NAO E' DONO DA PAUSA, O TETO E' (29/08/2026).
            #
            # `pausa_ate` e' a hora em que a janela viraria PARA O TETO QUE
            # VALIA NA HORA DA PAUSA. Se o dono subiu o teto na tela depois
            # disso, o trabalho ja cabe -- e ficar olhando um relogio velho
            # faria o ajuste dele so valer no dia seguinte, que e' o oposto
            # do que um botao de regulagem deve fazer.
            #
            # Entao pergunta-se ao pacer, que e' quem sabe: se nao estoura
            # mais, volta agora. Se ainda estoura, o relogio continua
            # mandando -- e nada e' tentado a toa.
            if falta > 0:
                cabe_agora = False
                try:
                    from app.ratepacer import SharedPacer
                    janela, _ = SharedPacer('strava').estourou()
                    cabe_agora = janela is None
                except Exception:
                    cabe_agora = False
                if not cabe_agora:
                    return None   # a janela do Strava ainda nao virou
                recado_volta = ('O teto de requisições foi ampliado e este '
                                'trabalho já cabe. Continuando de onde '
                                'parou...')
            else:
                recado_volta = ('A janela do Strava virou. Continuando de '
                                'onde parou...')
        else:
            boot = _energia.instante_do_boot()
            pausado_em = float(params.get('pausado_em') or 0)
            if boot is None or boot <= pausado_em:
                return None      # a maquina ainda nao desligou/voltou: espera
            recado_volta = ('O servidor voltou. Continuando a importacao de '
                            'onde parou...')
        params = dict(params)
        params.pop('pausa_programada', None)
        params.pop('pausado_em', None)
        params.pop('pausa_ate', None)
        params['owner_pid'] = os.getpid()
        _set_job_params(job_id, params)
        _set_progress(job_id, 'reading', recado_volta,
                      max(15, int(job.get('pct') or 15)))
        logger.warning('Job %s retomado: %s', job_id, recado_volta)
        if _lancar_job_em_processo(job_id, params) is not None:
            return _job_progress.get(job_id) or _read_progress_file(job_id)
        # sem processo proprio, segue pela thread, como sempre
        runner_pausa = {'upload_txt': _run_upload_job,
                        'importacao_atleta': _run_importacao_atleta,
                        'recuperacao_links': _run_recuperacao_links,
                        }.get(params.get('kind'))
        if runner_pausa is not None:
            app_atual = current_app._get_current_object()
            t = threading.Thread(target=runner_pausa, args=(app_atual, job_id, params),
                                 daemon=True)
            _LOCAL_THREADS[job_id] = t
            t.start()
        return _job_progress.get(job_id) or _read_progress_file(job_id)

    # DOIS TIPOS DE TRABALHO LONGO, NAO UM (14/08/2026). Antes so o upload de
    # conversa voltava; a importacao por perfil morria em cada reinicializacao
    # do worker — e o worker reinicia a cada arquivo salvo. As duas sao
    # idempotentes pela deduplicacao por id de atividade, entao retomar e
    # simplesmente rodar de novo com os mesmos parametros.
    _RUNNERS = {
        'upload_txt':        _run_upload_job,
        'importacao_atleta': _run_importacao_atleta,
        'recuperacao_links': _run_recuperacao_links,
    }
    kind = params.get('kind')
    runner = _RUNNERS.get(kind)
    if runner is None:
        return None
    age = _job_age_seconds(job)

    # IMPORTACAO DE ATLETA: PRAZO LONGO, E DESDE 20/08/2026 CONTADOR QUE ZERA
    # COM PROGRESSO DE VERDADE — mesma regra do upload_txt, agora que os dois
    # tem checkpoint granular.
    #
    # Ate 14/08/2026 a importacao por perfil NAO tinha checkpoint: retomar
    # refazia a listagem do perfil desde o primeiro mes, e o dono viu
    # "Lendo o perfil: 07/2021 (1/62)" -> "Retomando..." -> "(1/62)" de novo,
    # por quase uma hora, sem uma atividade sequer salva. A causa era dupla:
    # 150s e menos que a pausa normal do Strava quando o ritmo aperta (um job
    # SAUDAVEL era declarado morto), e cada "recomeco" produzia batimento
    # novo, zerando o contador — o freio nunca pegava.
    #
    # NAQUELE MOMENTO a correcao certa foi desligar o zerar (`pode_zerar =
    # False`): sem checkpoint, batimento novo nao provava progresso nenhum,
    # so um reinicio disfarcado. Mas `_listar_perfil_com_checkpoint` (mesmo
    # dia, mais tarde na sessao) ADICIONOU o checkpoint por mes — e esta
    # trava aqui ficou desatualizada, sem ninguem perceber, porque o sintoma
    # so aparece em importacao GRANDE (todos os periodos, 40-60 meses):
    # medido em 18/08 (job 3aebf957), a importacao foi retomada 3 vezes,
    # cada vez com progresso real (meses novos no checkpoint) — e na 3a vez
    # o teto (`_RESUME_MAX_ATTEMPTS`) bateu e ela morreu, silenciosa, sem
    # nenhum aviso na tela. Importacao curta (poucos meses) raramente bate
    # em 3 interrupcoes antes de terminar; a de 40-60 meses, sim — e cada
    # `wsgi.py` tocado durante o desenvolvimento (regra do CLAUDE.md — e o
    # UNICO gatilho de reload deste uWSGI, conferido em `uwsgi.ini`:
    # `touch-reload`) mata as threads de todos os jobs em andamento, contando
    # como uma interrupcao.
    #
    # Com o checkpoint por mes existindo de verdade, o mesmo raciocinio do
    # upload_txt vale aqui: 10+ batimentos novos desde a ultima retomada e
    # prova de progresso real (meses novos sendo lidos), nao um reinicio
    # disfarcado — e o contador pode voltar a zerar.
    if kind == 'importacao_atleta':
        limite_parado = _HEARTBEAT_STALE_ATLETA
        pode_zerar = True
    else:
        limite_parado = _HEARTBEAT_STALE_SECONDS
        pode_zerar = True

    if age is None or age <= limite_parado:
        return None
    thread = _LOCAL_THREADS.get(job_id)
    if thread is not None and thread.is_alive():
        return None
    # O JOB PODE ESTAR VIVO EM OUTRO PROCESSO DO uWSGI (20-21/08/2026).
    # `_LOCAL_THREADS` e memoria DE UM SO PROCESSO -- com `processes=2` no
    # uwsgi.ini, o ticker deste processo nao enxerga a thread que outro
    # worker iniciou, mesmo que ela esteja viva e trabalhando. Medido ao
    # vivo: 20 "retomadas" numa madrugada so, todas sem nenhum erro real —
    # a importacao do perfil <id-3> nunca chegou a morrer, so ficou
    # invisivel para o processo que checou. Antes de concluir "morto",
    # confere se o processo DONO ainda existe.
    owner_pid = params.get('owner_pid')
    if owner_pid and int(owner_pid) != os.getpid():
        # Idem: zumbi nao conta como vivo (ver `_processo_vivo`).
        if _processo_vivo(owner_pid):
            return None  # processo dono ainda de pe -- so nao ve a thread daqui
        # processo dono realmente morreu -- segue para reanimar
    resume_count = int(params.get('resume_count') or 0)
    seq_atual = int(job.get('seq') or 0)
    seq_ultima_retomada = params.get('resume_last_seq')
    # PROGRESSO SE MEDE EM TRABALHO CONCLUIDO, NUNCA EM BATIMENTO (20/08/2026).
    # Ate hoje bastava o job produzir 10 batimentos novos para as tentativas
    # zerarem — e um job que REFAZ os mesmos links produz batimento de sobra
    # sem concluir nada. Resultado medido: 41 retomadas num dia, todas
    # anotadas como "tentativa 1", laco infinito. Agora o que conta e o que
    # de fato avancou: meses PROCESSADOS (listados, raspados e salvos -- nao
    # so listados, ver 20/08/2026 mais tarde) e links resolvidos na
    # validacao. Job que congela sempre no mesmo ponto nao renova nada,
    # esgota as 3 tentativas e PARA — com aviso, que era o que faltava em
    # 14/08 (ERROS.md 4.14).
    trabalho_atual = (len(params.get('meses_processados') or [])
                      + len(params.get('links_concluidos') or []))
    trabalho_anterior = params.get('resume_last_trabalho')
    if (pode_zerar and trabalho_anterior is not None
            and trabalho_atual > int(trabalho_anterior)):
        resume_count = 0
    if resume_count >= _RESUME_MAX_ATTEMPTS:
        # DESISTIR EM SILENCIO FOI O DEFEITO DE 14/08 (ERROS.md 4.14): o job
        # parava de ser reanimado e ficava eternamente em "scraping" na tela,
        # sem erro, sem e-mail, sem nada. Agora ele termina como erro, dizendo
        # o que ficou e o que fazer — e o que ja foi salvo continua no ranking.
        if not params.get('desistencia_avisada'):
            params = dict(params)
            params['desistencia_avisada'] = True
            _set_job_params(job_id, params)
            feitos = len(params.get('links_concluidos') or [])
            _set_progress(
                job_id, 'error',
                'A leitura no Strava travou %d vezes seguidas no mesmo ponto e '
                'a importacao parou aqui. O que ja foi lido esta salvo '
                '(%d atividade(s) resolvida(s)). Repita a importacao deste '
                'atleta mais tarde: ela continua de onde parou.'
                % (_RESUME_MAX_ATTEMPTS, feitos),
                0,
                job.get('zip_filename', ''), job.get('cidade', ''),
            )
            logger.warning(
                'Job %s (%s) desistiu apos %s retomadas sem progresso real '
                '(%s links resolvidos)',
                job_id, params.get('kind'), _RESUME_MAX_ATTEMPTS, feitos)
        return None
    if not _claim_resume(job_id):
        return None

    params = dict(params)
    params['resume_count'] = resume_count + 1
    params['resume_last_seq'] = seq_atual
    params['resume_last_trabalho'] = trabalho_atual
    params['owner_pid'] = os.getpid()
    _set_job_params(job_id, params)
    # A FRASE TEM DE DIZER O QUE ACONTECEU. Ate hoje ela acusava "interrupcao
    # do servidor" em toda retomada, inclusive nas 41 de 20/08, em que nao
    # houve reinicio nenhum — o que travou foi uma requisicao ao Strava. Aviso
    # que aponta o culpado errado manda a pessoa procurar no lugar errado.
    _set_progress(
        job_id,
        'reading',
        'A leitura no Strava ficou %d minuto(s) sem responder. '
        'Retomando de onde parou (tentativa %d de %d).'
        % (max(1, int(age) // 60), resume_count + 1, _RESUME_MAX_ATTEMPTS),
        max(38, int(job.get('pct') or 38)),
    )
    app = current_app._get_current_object()
    thread = threading.Thread(target=runner, args=(app, job_id, params), daemon=True)
    _LOCAL_THREADS[job_id] = thread
    thread.start()
    logger.warning('Job %s (%s) retomado apos heartbeat parado (%ss, tentativa %s)',
                   job_id, params.get('kind'), int(age), params['resume_count'])
    return _job_progress.get(job_id) or _read_progress_file(job_id)


_RESUME_TICKER_STARTED = False


def _schedule_continuation(origin_job_id, params, continuation_n, cooldown):
    """Agenda a proxima rodada automatica de um processamento parcial."""
    novos_params = dict(params)
    novos_params['continuation'] = continuation_n
    novos_params['resume_count'] = 0
    novos_params.pop('resume_last_seq', None)
    payload = {
        'params': novos_params,
        'not_before': _time.time() + max(0, cooldown),
        'origin_job': origin_job_id,
    }
    try:
        _PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
        path = _PROGRESS_DIR / f'{uuid.uuid4()}.continue'
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=True)
        logger.info('Continuacao %s agendada em %ss (origem job=%s)',
                    continuation_n, int(cooldown), origin_job_id)
        return True
    except Exception as exc:
        logger.error('Falha ao agendar continuacao do job %s: %s', origin_job_id, exc)
        return False


def _cancel_city_continuations(cidade):
    """Remove continuacoes agendadas da cidade (um reenvio manual as substitui)."""
    try:
        for path in _PROGRESS_DIR.glob('*.continue'):
            try:
                with open(path, 'r', encoding='utf-8') as fh:
                    payload = json.load(fh)
                if ((payload.get('params') or {}).get('cidade') or '') == cidade:
                    path.unlink()
                    logger.info('Continuacao agendada de %s cancelada por reenvio manual', cidade)
            except OSError:
                pass
            except Exception:
                continue
    except Exception as exc:
        logger.debug('Falha ao limpar continuacoes de %s: %s', cidade, exc)


def _has_active_job_for_city(cidade):
    """True se ha job de upload vivo (nao terminal, heartbeat fresco) da cidade."""
    for path in _PROGRESS_DIR.glob('*.json'):
        job = _read_progress_file(path.stem)
        if not job or job.get('stage') in _TERMINAL_STAGES:
            continue
        if (job.get('cidade') or '') == cidade and not _is_stale_job(job):
            return True
    return False


def _scan_continuations():
    """Lanca rodadas de continuacao vencidas. Claim atomico via os.replace."""
    for path in _PROGRESS_DIR.glob('*.continue'):
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                payload = json.load(fh)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if _time.time() < float(payload.get('not_before') or 0):
            continue
        # Cidade com job ativo: espera a vez (nunca duas rodadas em paralelo).
        cidade_cont = ((payload.get('params') or {}).get('cidade') or '')
        if cidade_cont and _has_active_job_for_city(cidade_cont):
            continue
        claimed = Path(str(path) + '.claimed')
        try:
            os.replace(str(path), str(claimed))
        except OSError:
            continue  # outro worker levou
        try:
            params = payload.get('params') or {}
            if params.get('kind') != 'upload_txt':
                continue
            new_job_id = str(uuid.uuid4())
            rodada = params.get('continuation') or '?'
            _set_progress(new_job_id, 'reading',
                          f'Continuacao automatica do processamento (rodada {rodada})...',
                          38, params.get('zip_filename', ''), params.get('cidade', ''))
            params['owner_pid'] = os.getpid()
            _set_job_params(new_job_id, params)
            app = current_app._get_current_object()
            thread = threading.Thread(target=_run_upload_job,
                                      args=(app, new_job_id, params), daemon=True)
            _LOCAL_THREADS[new_job_id] = thread
            thread.start()
            logger.warning('Continuacao automatica lancada: job=%s rodada=%s origem=%s',
                           new_job_id, rodada, payload.get('origin_job'))
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass


# Quantos meses um trabalho da fila adianta por passada do ticker. Baixo de
# proposito: adiantar nao pode roubar a vez de quem ESTA rodando, e as duas
# coisas dividem a mesma fila de acesso ao Strava (SharedPacer).
_MESES_POR_PASSADA = 4
# So adianta com folga de cota. Chegar perto do teto por causa de trabalho
# adiantado seria trocar uma fila lenta por uma fila parada.
_FOLGA_MINIMA_PARA_ADIANTAR = 0.30


def _ha_folga_para_adiantar():
    """Sobra cota suficiente para adiantar trabalho sem atrapalhar a vez?"""
    try:
        from app.ratepacer import SharedPacer
        from app import stravasinal
        if stravasinal.bloqueado_ate():
            return False
        pacer = SharedPacer('strava')
        tetos = pacer.tetos()
        uso = pacer.uso()
        for chave in ('q15', 'hora', 'dia'):
            teto = int(tetos.get(chave) or 0)
            if teto <= 0:
                continue
            if (teto - int(uso.get(chave) or 0)) < teto * _FOLGA_MINIMA_PARA_ADIANTAR:
                return False
        return True
    except Exception:
        return False


def _adiantar_descoberta(job_id, job):
    """Le do Strava os meses deste trabalho ANTES de ele chegar a vez.

    E' o "descascar" pedido pelo dono (29/08/2026). Uma importacao tem duas
    fases: DESCOBERTA (uma requisicao por mes, que ja traz todas as
    atividades daquele mes) e GRAVACAO (banco, anticheat -- zero Strava).
    Quem espera na fila fica parado sem fazer nada; aqui ele adianta a
    primeira. Na vez dele, sobra so a segunda, que roda em segundos.

    NAO GASTA REQUISICAO A MAIS: o total e' o mesmo, so muda QUANDO. O que
    encurta de verdade e' o tempo com a vaga do servidor ocupada.

    Vai na ORDEM DA FILA (o proximo a subir descasca primeiro), e nao de
    tras para frente: assim o mais provavel de ser chamado e' o que ja esta
    pronto. Quem sobe com descasque parcial simplesmente continua o resto
    do jeito antigo -- nada se perde por estar pela metade.
    """
    params = job.get('params') or {}
    if params.get('kind') != 'importacao_atleta':
        return 0
    athlete_id = _id_do_trabalho(params)
    if not athlete_id:
        return 0
    cidade = job.get('cidade') or params.get('cidade')
    try:
        from app import descasque
        from app.blueprints.processing import (fetch_athlete_month_atividades,
                                               build_strava_throttle)
        cookie = secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)
        if not cookie:
            return 0
        periodos = _periodos_de_json(params.get('periodos'))
        ja_feitos = {tuple(p) for p in (params.get('meses_processados') or [])
                     if len(p) == 2}
        prontos = descasque.meses_prontos(job_id)
        candidatos = sorted(
            (m for m in (periodos or ()) if m not in ja_feitos and m not in prontos),
            reverse=True)
        if not candidatos:
            return 0
        # O PERFIL QUE O PROPRIO JOB PEDIU, senao o mais lento: adiantar e
        # trabalho de fundo e nao pode disputar espacamento com quem esta
        # na vez.
        throttle = build_strava_throttle(
            mode=params.get('throttle_profile') or 'ano_especifico',
            cidade=cidade, session_cookie=cookie)
        feitos = 0
        for ano, mes in candidatos[:_MESES_POR_PASSADA]:
            if not _ha_folga_para_adiantar():
                break
            entradas, total_km = fetch_athlete_month_atividades(
                athlete_id, ano, mes, cookie, throttle=throttle)
            descasque.guardar(job_id, ano, mes, entradas, total_km)
            feitos += 1
        if feitos:
            resumo = descasque.resumo(job_id)
            _set_progress(
                job_id, _ESTAGIO_NA_FILA,
                'Na fila — mas já adiantei %d mês(es) deste atleta '
                '(%d atividade(s) prontas). Quando chegar a vez, é só gravar.'
                % (resumo['meses'], resumo['atividades']), 4,
                job.get('zip_filename', ''), cidade)
        return feitos
    except Exception as exc:
        # ADIANTAR E' BONUS, NUNCA OBRIGACAO: se falhar, o trabalho roda do
        # jeito de sempre quando chegar a vez. Nao vira erro na tela.
        logger.info('Nao consegui adiantar %s: %s: %s',
                    job_id, type(exc).__name__, exc)
        return 0


def _scan_and_resume_jobs():
    """Varre os jobs persistidos em disco e reanima os que travaram."""
    if not _PROGRESS_DIR.exists():
        return
    _aguardando = []
    for path in _PROGRESS_DIR.glob('*.json'):
        job_id = _safe_job_id(path.stem)
        if not job_id:
            continue
        job = _read_progress_file(job_id)
        if not job or job.get('stage') in _TERMINAL_STAGES:
            continue
        # NA FILA: comeca sozinho quando abrir vaga (26/08/2026).
        # Aqui, e nao numa rotina nova: este ticker ja passa de 2 em 2 minutos
        # olhando os mesmos arquivos. Criar um segundo relogio para isso seria
        # gastar a maquina duas vezes pelo mesmo trabalho.
        if job.get('stage') == _ESTAGIO_NA_FILA:
            if not _ha_vaga_para_trabalho_pesado():
                # SEM VAGA NAO E' SEM TER O QUE FAZER (29/08/2026).
                # Ate aqui o trabalho na fila ficava parado ate abrir vaga.
                # Agora ele adianta a leitura dos meses -- a parte que custa
                # Strava -- para que, na vez dele, sobre so gravar.
                _aguardando.append((job_id, job))
                continue
            params_fila = job.get('params') or {}
            _set_progress(job_id, 'reading',
                          'Chegou a vez: %s. Comecando...'
                          % _ROTULO_TRABALHO.get(params_fila.get('kind'),
                                                 'trabalho longo').lower(), 5,
                          job.get('zip_filename', ''), job.get('cidade', ''))
            if _lancar_job_em_processo(job_id, params_fila) is None:
                app_atual = current_app._get_current_object()
                runner_fila = {'upload_txt': _run_upload_job,
                               'importacao_atleta': _run_importacao_atleta,
                               'recuperacao_links': _run_recuperacao_links,
                               'atualizar_perfis': _run_atualizar_perfis,
                               }.get(params_fila.get('kind'))
                if runner_fila is not None:
                    t = threading.Thread(target=runner_fila,
                                         args=(app_atual, job_id, params_fila),
                                         daemon=True)
                    _LOCAL_THREADS[job_id] = t
                    t.start()
            logger.info('Job %s saiu da fila e comecou', job_id)
            continue

        try:
            resumed = _maybe_resume_job(job_id, job)
            if resumed:
                logger.warning('Ticker reanimou job %s (sem polling do navegador)', job_id)
        except Exception as exc:
            logger.debug('Ticker: falha ao avaliar job %s: %s', job_id, exc)

    # AGORA OS QUE ESPERAM ADIANTAM A LEITURA (29/08/2026).
    # Depois de reanimar quem precisava, sobra tempo de ticker para quem
    # esta na fila descascar os proprios meses. Na ORDEM DE CHEGADA: o
    # proximo a subir e o que mais tem a ganhar em ja estar pronto.
    #
    # UM POR PASSADA, de proposito. O ticker volta em 2 minutos, e adiantar
    # o trabalho de todo mundo de uma vez so encheria a cota que o trabalho
    # EM ANDAMENTO precisa agora.
    if _aguardando and _ha_folga_para_adiantar():
        _aguardando.sort(key=lambda par: float((par[1] or {}).get('ts') or 0))
        for _jid, _job in _aguardando:
            if _adiantar_descoberta(_jid, _job):
                break

    _scan_continuations()


_FAXINA_TERMINAL_DIAS = 7
_FAXINA_CACHE_HORAS = 2          # o cache da conferencia vale 45 min (conferencia.py)
_FAXINA_ORFAO_DIAS = 3


def faxina_de_jobs():
    """Limpa a pasta de trabalhos. Roda 1x por dia, pela rotina diaria.

    POR QUE (medido em 26/08/2026)
    `temp_extract/jobs/` tinha 242 arquivos, o mais velho de junho, e NUNCA
    foi limpa. Pior: `_scan_and_resume_jobs` (de 2 em 2 min), o ticker de
    continuacoes e a tela de upload ABREM TODOS eles a cada passada -- numa
    maquina que passa 43% do tempo esperando disco, isso e' desperdicio que so
    cresce. Havia ainda 18 trabalhos "vivos" que nunca terminariam: 16 do cache
    da tela de Conferencia (que grava na mesma pasta) e 2 abandonados, um deles
    ha 33 dias.

    O cache da Conferencia agora grava em pasta propria (`jobstore.CACHE_DIR`,
    Passo 6 do plano `o-ranking-de-manh-ethereal-feather.md`): a pasta de
    trabalhos passa a ter so trabalho de verdade, e o ticker de 2 em 2 min nao
    perde mais tempo lendo cache. O laco abaixo ainda apaga `confcache_*` que
    sobrou de antes da migracao -- nada volta a escrever ali, entao e' faxina
    de uma vez so, nao recorrente.

    O QUE ELA NUNCA FAZ
    Nao encosta em trabalho vivo. O criterio e o mesmo que o resto do modulo ja
    usa para decidir se um trabalho morreu (`_is_stale_job`), entao nao existe
    aqui uma segunda opiniao que possa divergir da primeira.
    """
    if not _PROGRESS_DIR.exists():
        return {'apagados': 0, 'encerrados': 0, 'cache': 0}

    agora = _time.time()
    apagados = encerrados = cache = 0

    for caminho in list(_PROGRESS_DIR.glob('confcache_*.json')):
        # Resto de antes da migracao (26/08/2026): nada mais escreve cache
        # aqui, entao qualquer arquivo encontrado e' lixo velho.
        try:
            caminho.unlink()
            cache += 1
        except OSError:
            pass

    if jobstore.CACHE_DIR.exists():
        for caminho in list(jobstore.CACHE_DIR.glob('*.json')):
            try:
                with open(caminho, 'r', encoding='utf-8') as fh:
                    entrada = json.load(fh)
            except Exception:
                entrada = None
            marca = 0.0
            if isinstance(entrada, dict):
                try:
                    marca = float(entrada.get('cached_at') or 0)
                except (TypeError, ValueError):
                    marca = 0.0
            if not marca:
                marca = caminho.stat().st_mtime
            if agora - marca > _FAXINA_CACHE_HORAS * 3600:
                try:
                    caminho.unlink()
                    cache += 1
                except OSError:
                    pass

    for caminho in list(_PROGRESS_DIR.glob('*.json')):
        try:
            with open(caminho, 'r', encoding='utf-8') as fh:
                job = json.load(fh)
        except Exception:
            job = None

        if not isinstance(job, dict):
            continue

        estagio = job.get('stage')
        idade = _job_age_seconds(job)

        # --- trabalho concluido ha mais de uma semana: nao serve mais a ninguem
        if estagio in _TERMINAL_STAGES:
            if idade is not None and idade > _FAXINA_TERMINAL_DIAS * 86400:
                for irmao in ('.json', '.resume', '.continue', '.cancel'):
                    try:
                        caminho.with_suffix(irmao).unlink(missing_ok=True)
                    except (OSError, TypeError):
                        pass
                apagados += 1
            continue

        # --- abandonado: nao terminal, parado ha dias, sem ninguem para retomar
        if idade is not None and idade > _FAXINA_ORFAO_DIAS * 86400:
            origem = str(job.get('zip_filename') or '')
            e_de_upload = (origem.startswith('importacao_atleta_')
                           or (job.get('params') or {}).get('kind'))
            if not e_de_upload:
                # Conferencia (`conf_`) e manutencao (`maint_`) gravam nesta
                # mesma pasta e ninguem espera resposta deles dias depois.
                # Escrever "esta importacao parou" num job de conferencia seria
                # mentir na tela; some com o arquivo e pronto.
                try:
                    caminho.unlink(missing_ok=True)
                except (OSError, TypeError):
                    pass
            elif not (job.get('params') or {}).get('kind'):
                # E de upload, mas sem parametros gravados: nada pode continuar
                # de onde parou, e quem voltar a tela precisa entender por que.
                _encerrar_importacao_orfa(caminho.stem, job)
            else:
                _set_progress(
                    caminho.stem, 'error',
                    'Este trabalho ficou %d dia(s) sem responder e foi encerrado '
                    'pela faxina. O que ja tinha sido salvo continua no ranking; '
                    'repetir a importacao continua de onde parou.' % (idade // 86400),
                    0, origem, job.get('cidade', ''),
                )
            encerrados += 1

    # Recolhe os processos de trabalho longo que ja terminaram, para nao
    # deixarem zumbi atras (ver `_processo_vivo`). `poll()` nao bloqueia.
    for jid, proc in list(_PROCESSOS_FILHOS.items()):
        try:
            if proc.poll() is not None:
                _PROCESSOS_FILHOS.pop(jid, None)
        except Exception:
            _PROCESSOS_FILHOS.pop(jid, None)

    # O log dos processos de trabalho longo tambem nao pode crescer sem fim.
    # Corte simples: passou de 1 MB, guarda uma copia e recomeca. Uma copia
    # basta -- este arquivo so tem valor quando um arranque falha, e isso se
    # descobre no mesmo dia.
    try:
        if _LOG_JOBRUNNER.exists() and _LOG_JOBRUNNER.stat().st_size > _LOG_JOBRUNNER_MAX:
            anterior = _LOG_JOBRUNNER.with_suffix('.log.1')
            os.replace(str(_LOG_JOBRUNNER), str(anterior))
            logger.info('Log dos trabalhos longos passou de 1 MB e foi recomecado')
    except OSError as exc:
        logger.debug('Nao consegui cortar o log dos trabalhos longos: %s', exc)

    # O DESCASQUE SEGUE O TRABALHO (29/08/2026): quando o job some, o que
    # foi adiantado para ele nao serve mais para ninguem. Sem isto a pasta
    # cresceria calada, como `temp_extract/jobs/` cresceu ate 242 arquivos.
    descascados = 0
    try:
        from app import descasque
        vivos = []
        if _PROGRESS_DIR.exists():
            vivos = [p.stem for p in _PROGRESS_DIR.glob('*.json')]
        descascados = descasque.faxina(vivos)
    except Exception as exc:
        logger.debug('Faxina do descasque: %s', exc)

    # LOG VELHO TAMBEM E' LIXO (07/09/2026, regra 19). A politica do dono e'
    # "60 dias completos, depois um arquivo por mes": aqui saem so as SOBRAS de
    # tamanho (`...-2026-07.log.3`) que ja passaram do prazo. O arquivo do mes
    # nunca sai por conta propria. Pendurado nesta faxina porque ela ja roda
    # sozinha e ja tem o habito de nao derrubar nada quando falha.
    logs_removidos, logs_bytes = 0, 0
    try:
        from app import logmensal
        pasta_log = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'logs')
        logs_removidos, logs_bytes = logmensal.expurgar(pasta_log)
    except Exception as exc:
        logger.debug('Faxina de logs: %s', exc)

    if apagados or encerrados or cache or descascados or logs_removidos:
        logger.info('Faxina de jobs: %s concluido(s) apagado(s), %s abandonado(s) '
                    'encerrado(s), %s cache(s) vencido(s) removido(s), '
                    '%s descasque(s) orfao(s) removido(s), '
                    '%s log(s) vencido(s) removido(s) (%.1f MB)',
                    apagados, encerrados, cache, descascados,
                    logs_removidos, logs_bytes / 1048576.0)
    return {'apagados': apagados, 'encerrados': encerrados, 'cache': cache,
            'descasque': descascados, 'logs': logs_removidos}


def start_resume_ticker(app):
    """Thread de fundo que retoma jobs interrompidos sem depender do navegador.

    Antes, um job so era reanimado quando alguem mantinha a pagina de upload
    aberta (o watchdog rodava no polling de status). Com o ticker, um backfill
    longo sobrevive a quedas de worker e reinicios mesmo com o navegador
    fechado. O claim atomico em _maybe_resume_job impede retomada dupla com
    mais de um worker uWSGI rodando o ticker.
    """
    global _RESUME_TICKER_STARTED
    if _RESUME_TICKER_STARTED:
        return
    _RESUME_TICKER_STARTED = True

    def _tick():
        _time.sleep(90)  # deixa o worker estabilizar apos o boot
        while True:
            try:
                with app.app_context():
                    _scan_and_resume_jobs()
            except Exception as exc:
                logger.debug('Ticker de retomada: %s', exc)
            _time.sleep(_RESUME_TICKER_INTERVAL)

    threading.Thread(target=_tick, daemon=True, name='resume-ticker').start()


def _find_processed_file_for_job(job_id, job=None):
    job = job or {}
    pf = (
        ProcessedFile.query
        .filter(ProcessedFile.mensagem_erro.like(f'%{job_id}%'))
        .order_by(ProcessedFile.id.desc())
        .first()
    )
    if pf:
        return pf

    zip_filename = job.get('zip_filename')
    cidade = job.get('cidade')
    if zip_filename and cidade:
        return (
            ProcessedFile.query
            .filter_by(arquivo_nome=zip_filename, cidade=cidade)
            .order_by(ProcessedFile.id.desc())
            .first()
        )
    return None


def _activity_ids_for_upload(pf_id=None, zip_filename='', cidade=''):
    ids = set()
    if pf_id:
        ids.update(
            row[0] for row in db.session.query(Activity.id)
            .filter(Activity.processed_file_id == pf_id)
            .all()
        )
    if zip_filename and cidade:
        ids.update(
            row[0] for row in db.session.query(Activity.id)
            .filter(Activity.arquivo_origem == zip_filename, Activity.cidade == cidade)
            .all()
        )
    return list(ids)


def _undo_flag_disciplinary_counts(flags, activity_by_id):
    for flag in flags:
        atividade = activity_by_id.get(flag.atividade_id)
        if not atividade or not atividade.data_hora:
            continue
        mes_ano = atividade.data_hora.strftime('%Y-%m')
        cd = ControleDisciplinar.query.filter_by(
            atleta_id=flag.atleta_id,
            mes_ano=mes_ano,
        ).first()
        if not cd:
            continue

        cd.total_flags = max(0, (cd.total_flags or 0) - 1)
        removed_by_flag = (
            atividade.status == 'removida' or
            f'{flag.tipo_flag}_REMOVIDA' in (atividade.strava_flags or '')
        )
        if removed_by_flag:
            cd.remocoes = max(0, (cd.remocoes or 0) - 1)
        else:
            cd.avisos = max(0, (cd.avisos or 0) - 1)
            cd.atividades_suspeitas = max(0, (cd.atividades_suspeitas or 0) - 1)

        cd.eliminado_do_ranking = (
            (cd.avisos or 0) >= 4 or
            (cd.total_flags or 0) >= 5 or
            (cd.atividades_suspeitas or 0) >= 5
        )


def _delete_upload_activities(pf_id=None, zip_filename='', cidade=''):
    activity_ids = _activity_ids_for_upload(pf_id, zip_filename, cidade)
    if not activity_ids:
        return 0

    activities = Activity.query.filter(Activity.id.in_(activity_ids)).all()
    activity_by_id = {a.id: a for a in activities}
    flags = FlagAnticheat.query.filter(
        FlagAnticheat.atividade_id.in_(activity_ids)
    ).all()
    flag_ids = [f.id for f in flags]

    if flag_ids:
        EvidenciaRevisao.query.filter(
            EvidenciaRevisao.flag_id.in_(flag_ids)
        ).delete(synchronize_session=False)
        _undo_flag_disciplinary_counts(flags, activity_by_id)
        FlagAnticheat.query.filter(
            FlagAnticheat.id.in_(flag_ids)
        ).delete(synchronize_session=False)

    Activity.query.filter(Activity.id.in_(activity_ids)).delete(synchronize_session=False)
    return len(activity_ids)


def _finalize_stale_cancel(job_id, delete=False, job=None):
    # SEMPRE avisa qualquer thread que ainda possa estar viva, ANTES de
    # qualquer outra coisa (22/08/2026). `_is_stale_job` decide "parece
    # parado" pelo heartbeat, sem checar se a thread deste job (mesmo
    # processo) esta mesmo morta -- se estiver so lenta, ela nunca soube que
    # devia parar, e a proxima vez que ela chamasse `_set_progress` reescrevia
    # por cima do "cancelled" que este metodo grava mais abaixo. Gravar aqui e
    # idempotente (os 3 chamadores existentes so entram aqui quando o job ja
    # esta/vai ficar cancelado mesmo).
    _cancel_flags[job_id] = {'delete': delete}
    _write_cancel_file(job_id, {'delete': delete})
    job = job or _job_progress.get(job_id) or _read_progress_file(job_id) or {}
    # O mesmo cuidado do paragrafo acima, para o trabalho que roda em processo
    # proprio: aviso nao basta, tem de haver quem confirme a parada.
    _garantir_parada(job_id, job)
    pf = _find_processed_file_for_job(job_id, job)
    zip_filename = (pf.arquivo_nome if pf else job.get('zip_filename')) or ''
    cidade = (pf.cidade if pf else job.get('cidade')) or ''
    pf_id = pf.id if pf else None
    activity_ids = _activity_ids_for_upload(pf_id, zip_filename, cidade)

    if delete:
        deleted = _delete_upload_activities(pf_id, zip_filename, cidade)
        total_kept = 0
        valid_kept = 0
        message = f'Processamento travado cancelado. {deleted} atividades parciais removidas do banco.'
        erro = 'Cancelado pelo usuario apos job ficar sem resposta (atividades removidas)'
    else:
        total_kept = len(activity_ids)
        valid_kept = 0
        if activity_ids:
            valid_kept = (
                Activity.query
                .filter(Activity.id.in_(activity_ids))
                .filter((Activity.status.is_(None)) | (Activity.status != 'removida'))
                .count()
            )
        message = (
            f'Processamento travado cancelado. {total_kept} atividades ja salvas '
            f'foram mantidas ({valid_kept} validas para o ranking).'
        )
        erro = 'Cancelado pelo usuario apos job ficar sem resposta'

    if pf:
        pf.total_atividades = total_kept
        pf.atividades_validas = valid_kept
        pf.status = 'cancelado'
        pf.mensagem_erro = erro
        pf.tempo_processamento = 0

    db.session.commit()
    if cidade:
        _invalidar_consolidacao(cidade)
    _delete_cancel_file(job_id)
    _set_progress(job_id, 'cancelled', message, 0, zip_filename, cidade)
    _expire_job(job_id, 24 * 60 * 60)
    logger.warning(
        'Job travado finalizado por cancelamento: job=%s delete=%s pf=%s atividades=%s',
        job_id, delete, pf_id, len(activity_ids)
    )
    return message


def _periodos_das_atividades(cidade, processed_file_id=None):
    query = Activity.query.filter(Activity.cidade == cidade)
    if processed_file_id is not None:
        query = query.filter(Activity.processed_file_id == processed_file_id)
    periodos = set()
    for atividade in query.filter(Activity.data_hora.isnot(None)).all():
        periodos.add((atividade.data_hora.year, atividade.data_hora.month))
    return periodos


def _invalidar_consolidacao(cidade, periodos=None):
    """Atualiza snapshots nos periodos afetados, sem depender do mes do relogio."""
    try:
        from app.models import Consolidacao
        from app.blueprints.ranking import rebuild_monthly_backup
        if periodos is None:
            now = datetime.now()
            periodos = {(now.year, now.month)}
        periodos = {(int(ano), int(mes)) for ano, mes in periodos}
        for ano, mes in periodos:
            Consolidacao.query.filter_by(
                periodo=f'{ano}-{mes:02d}', tipo='mensal'
            ).delete()
        db.session.commit()
        for ano, mes in sorted(periodos):
            rebuild_monthly_backup(cidade, mes, ano)
        logger.info('Consolidacao e ranking atualizados para %s/%s', cidade, sorted(periodos))
    except Exception as e:
        db.session.rollback()
        logger.warning(f'Falha ao invalidar consolidacao: {e}')


def _recover_stuck_uploads():
    """Detecta processamentos interrompidos por restart e limpa dados parciais."""
    try:
        _ensure_upload_schema()
        now = datetime.now()
        stuck = ProcessedFile.query.filter_by(status='processando').all()
        recovered = 0
        still_fresh = 0
        recovered_cidades = set()
        for pf in stuck:
            job_id = _extract_job_id_from_message(pf.mensagem_erro)
            job = _read_progress_file(job_id) if job_id else None
            cancel_request = _read_cancel_file(job_id) if job_id else None

            if cancel_request is not None:
                _finalize_stale_cancel(
                    job_id,
                    delete=bool(cancel_request.get('delete', False)),
                    job=job,
                )
                recovered += 1
                continue

            process_age = None
            if pf.processado_em:
                process_age = (now - pf.processado_em).total_seconds()
            job_is_fresh = job is not None and not _is_stale_job(job)
            record_is_fresh = (
                job is None and process_age is not None and
                process_age <= _STALE_JOB_SECONDS
            )
            job_age = _job_age_seconds(job) if job else None
            job_is_resumable = (
                job is not None
                and job.get('stage') not in _TERMINAL_STAGES
                and (job.get('params') or {}).get('kind') == 'upload_txt'
                and job_age is not None and job_age <= _RESUME_WINDOW_SECONDS
            )
            if job_is_fresh or record_is_fresh or job_is_resumable:
                still_fresh += 1
                continue

            # Preserva o que ja foi salvo: as atividades sao validas e
            # deduplicadas; reenviar o arquivo continua de onde parou.
            mantidas = len(_activity_ids_for_upload(pf.id, pf.arquivo_nome, pf.cidade))
            pf.status = 'erro'
            pf.mensagem_erro = (
                'Interrompido por reinicializacao do servidor '
                f'({mantidas} atividades ja salvas foram mantidas; '
                'reenvie o arquivo para continuar)'
            )
            pf.total_atividades = mantidas
            pf.atividades_validas = mantidas
            if job_id:
                _delete_progress_file(job_id)
            recovered += 1
            recovered_cidades.add(pf.cidade)
            logger.warning(
                f'Startup recovery: ProcessedFile {pf.id} ({pf.arquivo_nome}) '
                f'estava processando -- {mantidas} atividades salvas mantidas'
            )
        if recovered:
            db.session.commit()
            for cidade in recovered_cidades:
                _invalidar_consolidacao(cidade)
            logger.info(f'Recovery concluido: {recovered} uploads interrompidos tratados')
        if still_fresh:
            logger.info(
                f'Recovery preservou {still_fresh} upload(s) ainda dentro da janela ativa'
            )
    except Exception as e:
        db.session.rollback()
        logger.error(f'Erro em _recover_stuck_uploads: {e}')


# ===== HELPERS =====

def _is_wa_txt(filename):
    fn = filename.lower()
    return fn.endswith('.txt') and any(kw in fn for kw in WA_TXT_KEYWORDS)


def _looks_like_text_file(filepath):
    try:
        with open(filepath, 'rb') as fh:
            sample = fh.read(4096)
    except OSError:
        return False
    if b'\x00' in sample:
        return False
    if not sample:
        return True
    try:
        sample.decode('utf-8')
        return True
    except UnicodeDecodeError:
        try:
            sample.decode('latin-1')
            return True
        except UnicodeDecodeError:
            return False


def _storage_slug(value, fallback):
    return secure_filename(str(value or '')) or fallback


def _file_hash(filepath):
    h = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_temp(temp_folder):
    try:
        for entry in os.listdir(temp_folder):
            path = os.path.join(temp_folder, entry)
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path) and entry != 'chunks':
                shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Erro ao limpar temp_folder: {e}")


def _cleanup_old_txts(txt_folder, max_files=500):
    try:
        all_txts = []
        for fn in os.listdir(txt_folder):
            if fn.endswith('.txt'):
                fp = os.path.join(txt_folder, fn)
                all_txts.append((os.path.getmtime(fp), fp))
        all_txts.sort(reverse=True)
        for _, fp in all_txts[max_files:]:
            os.remove(fp)
    except Exception as e:
        logger.warning(f"Erro ao limpar TXTs antigos: {e}")


def _validate_csrf():
    from flask_wtf.csrf import validate_csrf
    from wtforms import ValidationError
    token = _req.form.get('csrf_token') or _req.headers.get('X-CSRFToken', '')
    try:
        validate_csrf(token)
        return None
    except ValidationError as e:
        return jsonify(status='error', message=f'Token CSRF invalido: {e}'), 400


def _extract_manual_strava_urls(raw_values):
    urls = []
    seen = set()

    for raw in raw_values:
        text = (raw or '').strip()
        if not text:
            continue

        for match in STRAVA_DIRECT_RE.finditer(text):
            url = _sanitize_strava_activity_url(match.group(0), keep_share_sig=True)
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        for match in STRAVA_SHORT_RE.finditer(text):
            url = match.group(0).strip().rstrip('.,;)]}>')
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

    return urls


def _process_single_strava_url(raw_url, cidade, admin_id, only_current_month=True,
                               periodos=None, origem='link_avulso_admin',
                               throttle=None, excluir_periodos=None,
                               atleta_esperado=None, dados_prontos=None,
                               donos_aceitos=None):
    original_url = raw_url
    url = raw_url
    if 'strava.app.link' in url:
        resolved = resolve_strava_shortlink(url)
        if not resolved:
            return {
                'url': original_url,
                'status': 'erro',
                'message': 'Nao foi possivel resolver o link encurtado.',
            }
        url = resolved

    match = re.search(r'/activities/(\d+)', url or '')
    if not match:
        return {'url': original_url, 'status': 'erro', 'message': 'Link Strava invalido.'}

    activity_id = match.group(1)
    if Activity.query.filter_by(id_atividade=activity_id, cidade=cidade).first():
        return {
            'url': original_url,
            'status': 'duplicado',
            'message': f'Atividade {activity_id} ja existe no ranking desta cidade.',
        }

    # COM COOKIE, SEMPRE (27/08/2026, regra do dono). Sem sessao o Strava
    # devolve HTTP 200 com a casca de login e a atividade parece ilegivel --
    # foi assim que 76 atividades de uma noite viraram "nao pude ler".
    # COM THROTTLE, quando quem chama esta num LACO (27/08/2026). Sem isto
    # `conferenciaauto` e a recuperacao disparavam uma requisicao atras da
    # outra: a regra 4-C furada por dentro, porque o ban do Strava nao
    # pergunta por qual funcao a requisicao passou.
    # QUANDO A LISTAGEM JA TROUXE OS NUMEROS, NAO SE PEDE DE NOVO
    # (28/08/2026). `dados_prontos` vem do proprio /interval, no mesmo formato
    # que a raspagem devolveria. Entrar por aqui, e nao por um segundo
    # caminho, e' o que mantem deduplicacao, guarda do atleta, anti-cheat e
    # gravacao com UMA implementacao so.
    if dados_prontos:
        act_data = dict(dados_prontos)
    else:
        act_data = parse_strava_activity(
            url, throttle=throttle,
            session_cookie=secretstore.get(secretstore.STRAVA_SESSION,
                                           scope=cidade))
    if not act_data:
        return {
            'url': original_url,
            'status': 'erro',
            'message': 'Nao foi possivel obter os dados publicos da atividade.',
        }

    dt = act_data.get('data_hora')
    now = datetime.now()
    periodos_alvo = periodos
    if periodos_alvo is None and only_current_month:
        periodos_alvo = {(now.year, now.month)}
    if periodos_alvo is not None and (not dt or (dt.year, dt.month) not in periodos_alvo):
        periodo = dt.strftime('%m/%Y') if dt else 'desconhecido'
        return {
            'url': original_url,
            'status': 'ignorado',
            'motivo': 'outro_periodo',
            'message': f'Atividade fora do periodo selecionado ({periodo}).',
        }

    # O MES VIGENTE NAO E' ASSUNTO DA RECUPERACAO (regra do dono, 27/08/2026).
    # Recuperacao mexe em historico; o mes aberto se sustenta pelo link postado
    # no grupo (regra 4-A) e pela conferencia do proprio mes. Duas maos no mesmo
    # mes so criam disputa sobre quem contou o que.
    if excluir_periodos and dt and (dt.year, dt.month) in set(excluir_periodos):
        return {
            'url': original_url,
            'status': 'ignorado',
            'motivo': 'mes_vigente',
            'message': ('Atividade do mes vigente (%s) — fica para o '
                        'processamento do mes.' % dt.strftime('%m/%Y')),
        }

    # A GUARDA DO ATLETA VEM ANTES DE GRAVAR (27/08/2026, medido ao vivo).
    #
    # A importacao grava tudo e limpa o que nao e' do atleta no FIM. Para ela
    # funciona: e' um lote so, e a limpeza fecha antes de alguem ver. Para a
    # recuperacao NAO funcionou: em 12 minutos entraram 17 atletas estrangeiros
    # no cadastro (nomes de gente de outros paises...) e 18 atividades de
    # gente que nao e' do grupo -- porque a lista vem do FEED DE PERFIL, que
    # traz treino em grupo de terceiro (regra 4-D), e o feed de 2017 traz
    # desconhecido do mundo inteiro.
    #
    # "Cada km deve ter seu respectivo atleta sem duvida" (dono, 27/08). Duvida
    # nao se limpa depois: nao se cria.
    dono_real = str(act_data.get('id_usuario_strava') or '')
    if atleta_esperado and dono_real != str(atleta_esperado):
        return {
            'url': original_url,
            'status': 'ignorado',
            'motivo': 'outro_atleta',
            'message': ('Atividade de outra pessoa (perfil %s) — treino em '
                        'grupo, não entra no total deste atleta.' % (dono_real or '?')),
        }

    # QUEM MANDA E' O DONO DA ATIVIDADE (regra do dono, 27/08/2026).
    #
    # Treino em grupo NAO e' fraude nem duplicata: quando o Strava falha no
    # telefone de um dos dois, os km valem para os dois. Entao a atividade de
    # um parceiro que ESTA no cadastro entra normalmente, creditada a ele --
    # nao se mistura anti-cheat com importacao de atleta.
    #
    # O corte e' outro: quem NAO esta no cadastro nao entra, porque e' so um
    # amigo de quem marcou. Barrar aqui e' o que impede o que aconteceu em
    # 27/08, quando 17 estranhos (nomes de gente de outros paises...)
    # viraram atletas de Palmeiropolis em 12 minutos.
    if donos_aceitos is not None and dono_real not in donos_aceitos:
        return {
            'url': original_url,
            'status': 'ignorado',
            'motivo': 'dono_nao_cadastrado',
            'dono': dono_real,
            'dono_nome': act_data.get('nome_usuario') or '',
            'distancia': act_data.get('distancia') or 0,
            'message': ('Dono (%s, perfil %s) não está no cadastro desta '
                        'cidade — não entra.'
                        % (act_data.get('nome_usuario') or '?', dono_real or '?')),
        }

    modalidade = act_data.get('modalidade', '')
    distancia = act_data.get('distancia', 0)
    if modalidade not in ('ciclismo', 'corrida_caminhada') or distancia <= 0:
        return {
            'url': original_url,
            'status': 'ignorado',
            'motivo': 'modalidade',
            'message': f'Modalidade/distancia invalida para o ranking: {modalidade}, {distancia}km.',
        }

    new_act = Activity(
        id_atividade=act_data['id_atividade'],
        id_usuario_strava=act_data.get('id_usuario_strava', '0'),
        nome_usuario=act_data.get('nome_usuario', 'Desconhecido'),
        modalidade=modalidade,
        esporte_strava=act_data.get('esporte_strava'),
        distancia=distancia,
        data_hora=dt,
        cidade=cidade,
        id_admin_upload=admin_id,
        # QUEM GRAVOU ISTO: PESSOA OU MAQUINA? (27/08/2026)
        # Ate hoje tudo que passava por aqui saia como 'link_avulso_admin',
        # o rotulo de quando um admin cola link na mao. A conferencia
        # automatica reusa esta funcao e herdava o rotulo: o dono viu 14
        # atividades gravadas as 03:44 da madrugada, sem ninguem no
        # sistema, e nao tinha como saber pelo banco que fora a maquina.
        # O padrao segue sendo o humano; quem e automatico se declara.
        arquivo_origem=origem,
        link_original=url,
        gps_fingerprint=act_data.get('gps_fingerprint'),
        telefone_whatsapp=None,
        tempo_minutos=act_data.get('tempo_minutos'),
        elevacao_total=act_data.get('elevacao_total'),
        velocidade_media=act_data.get('velocidade_media'),
        pace_min_km=act_data.get('pace_min_km'),
        distancia_raw=act_data.get('distancia_raw'),
        processed_file_id=None,
    )

    db.session.add(new_act)
    db.session.flush()

    flags_count = 0
    try:
        from app.blueprints.anticheat import process_activity_anticheat
        flags = process_activity_anticheat(new_act)
        flags_count = len(flags or [])
    except ImportError:
        flags_count = 0
    except Exception as exc:
        logger.error('Erro anti-cheat para link avulso %s: %s', activity_id, exc, exc_info=True)

    db.session.commit()
    return {
        'url': original_url,
        'status': 'salvo',
        # A DATA VOLTA JUNTO (29/08/2026): quem chamou precisa dela para
        # saber se esta atividade e' anterior ou posterior a ultima leitura
        # do grupo -- e' o que separa "o WhatsApp perdeu" de "ainda nao foi
        # lido". Sem isso a conferencia acusava falha em link de 3 horas
        # atras (ver conferenciaauto.ate_quando_o_grupo_foi_lido).
        'data_hora': dt,
        'message': (
            f"{act_data.get('nome_usuario', 'Atleta')} | "
            f"{distancia:.1f}km | {modalidade} | atividade {activity_id}"
        ),
        'flags_count': flags_count,
        'periodo': (dt.year, dt.month) if dt else None,
    }



def _donos_aceitos(cidade, athlete_id=None):
    """Quem pode receber km nesta cidade: os atletas cadastrados, e o alvo.

    O alvo entra SEMPRE, mesmo sem cadastro ainda: importar o perfil dele foi
    um pedido explicito do admin, e a primeira importacao de um atleta novo
    acontece justamente antes de ele existir na tabela.
    """
    from app.models import Atleta
    aceitos = set()
    try:
        for (strava_id,) in db.session.query(Atleta.strava_id).filter(
                db.or_(Atleta.arquivado.is_(None), Atleta.arquivado == False)):  # noqa: E712
            if strava_id:
                aceitos.add(str(strava_id))
    except Exception as exc:
        logger.error('Nao consegui montar a lista de donos aceitos: %s', exc,
                     exc_info=True)
        return None          # None = sem corte, o comportamento antigo
    if athlete_id:
        aceitos.add(str(athlete_id))
    return aceitos


def _gravar_mes_pela_listagem(athlete_id, ano, mes, cidade, admin_id, origem,
                              session_cookie, throttle, donos_aceitos,
                              cancel_check=None, ids_ja_tentados=None,
                              on_link_concluido=None, progress_callback=None,
                              terceiros=None, job_id=None):
    """Um mes do perfil, do /interval direto para o banco. UMA requisicao.

    Devolve o MESMO dicionario que `process_whatsapp_txt` devolveria, para o
    laco que chama nao precisar saber por qual caminho o mes veio.
    """
    contagem = {
        'activities_processed': 0, 'activities_skipped': 0,
        'activities_failed': 0, 'ignoradas_outro_atleta': 0,
        'ignoradas_outro_periodo': 0, 'ignoradas_modalidade': 0,
    }
    pendentes = []
    ja_tentados = set(ids_ja_tentados or ())

    # A DESCOBERTA PODE JA TER SIDO FEITA NA FILA (29/08/2026).
    # Enquanto este trabalho esperava a vez, o ticker adiantou a leitura
    # deste mes e guardou o resultado. Reaproveitar aqui nao economiza
    # requisicao nenhuma -- ela ja foi gasta -- mas devolve a vaga do
    # servidor muito mais rapido, que era o que travava a fila.
    adiantado = None
    if job_id:
        try:
            from app import descasque
            adiantado = descasque.pegar(job_id, ano, mes)
        except Exception as exc:
            logger.debug('Sem descasque para %s %02d/%d: %s', job_id, mes, ano, exc)
    if adiantado:
        entradas, total_km = adiantado
        logger.info('Mes %02d/%d ja estava descascado (%d atividade(s)) — '
                    'gravando sem pedir nada ao Strava.',
                    mes, ano, len(entradas))
    else:
        entradas, total_km = fetch_athlete_month_atividades(
            athlete_id, ano, mes, session_cookie, throttle=throttle)

    total = len(entradas)
    if progress_callback:
        progress_callback('scraping',
                          '%d atividade(s) na listagem (%.1f km no total do mes).'
                          % (total, total_km or 0.0), 5)

    for indice, entrada in enumerate(entradas, 1):
        if cancel_check and cancel_check():
            return dict(contagem, cancelled=True, pending_details=pendentes,
                        total_listadas=total, total_km=total_km)
        act_id = entrada['id_atividade']
        if act_id in ja_tentados:
            continue
        url = 'https://www.strava.com/activities/%s' % act_id
        try:
            item = _process_single_strava_url(
                url, cidade=cidade, admin_id=admin_id,
                only_current_month=False, periodos={(ano, mes)},
                origem=origem, throttle=throttle,
                # A GUARDA E' A DO DONO, nao a do alvo: um parceiro que esta
                # no cadastro leva os km dele (regra do dono, 27/08).
                atleta_esperado=None,
                donos_aceitos=donos_aceitos,
                dados_prontos=entrada,
            )
        except Exception as exc:
            contagem['activities_failed'] += 1
            pendentes.append({'url': url, 'activity_id': act_id,
                              'reason': str(exc)[:160], 'stage': 'gravacao',
                              'retryable': True})
            logger.warning('Listagem %02d/%d: %s levantou %s',
                           mes, ano, act_id, exc)
            continue

        situacao = item.get('status')
        motivo = item.get('motivo') or ''
        if situacao == 'salvo':
            contagem['activities_processed'] += 1
        elif situacao == 'duplicado':
            contagem['activities_skipped'] += 1
        elif situacao == 'ignorado':
            if motivo == 'dono_nao_cadastrado':
                contagem['ignoradas_outro_atleta'] += 1
                # O RELATORIO NOMEIA QUEM FOI BARRADO (28/08/2026). Sem isto o
                # dono nao teria como perceber, por exemplo, um atleta da
                # cidade que refez o perfil e por isso aparece com outro id:
                # a atividade seria descartada com razao e em silencio.
                if terceiros is not None and item.get('dono'):
                    ficha = terceiros.setdefault(
                        str(item['dono']),
                        {'nome': item.get('dono_nome') or '', 'n': 0, 'km': 0.0})
                    ficha['n'] += 1
                    ficha['km'] = round(
                        ficha['km'] + float(item.get('distancia') or 0), 2)
            elif motivo == 'outro_atleta':
                contagem['ignoradas_outro_atleta'] += 1
            elif motivo == 'modalidade':
                contagem['ignoradas_modalidade'] += 1
            else:
                contagem['ignoradas_outro_periodo'] += 1
        else:
            contagem['activities_failed'] += 1
            pendentes.append({'url': url, 'activity_id': act_id,
                              'reason': item.get('message') or 'erro',
                              'stage': 'gravacao', 'retryable': True})

        if on_link_concluido:
            on_link_concluido(act_id, situacao)
        if progress_callback and total:
            progress_callback('reading',
                              'gravando %d de %d...' % (indice, total),
                              int(100 * indice / total))

    return dict(contagem, cancelled=False, pending_details=pendentes,
                deadline_hit=False, rate_limited=False,
                reconciliacao_pendente=0,
                total_listadas=total, total_km=total_km)


def _run_upload_job(app, job_id, params):
    """Corpo do job de upload. Roda em thread e e retomavel pelos params do job.

    Extraido da antiga closure _background_process sem mudanca de logica; os
    parametros vem do arquivo do job para permitir retomada apos reload.
    """
    txt_destinos = list(params.get('txt_paths') or [])
    zip_filename = params.get('zip_filename') or ''
    cidade       = params.get('cidade') or ''
    process_cm   = bool(params.get('process_cm'))
    admin_id     = params.get('admin_id')
    pf_id        = params.get('pf_id')
    throttle_profile = params.get('throttle_profile')
    periodos_raw = params.get('periodos')
    periodos = (
        {(int(ano), int(mes)) for ano, mes in periodos_raw}
        if periodos_raw else None
    )

    def _progress(stage, message, pct):
        _set_progress(job_id, stage, message, pct)

    try:
        with app.app_context():
            txt_folder = app.config['UPLOAD_FOLDER_TXT']
            total_links = total_processed = total_skipped = 0
            total_links_mes = total_pendentes = 0
            total_links_scrapeados = 0
            total_pend_retry = total_pend_perm = 0
            total_shortlinks_resolvidos = 0
            pending_details = []
            start_time = datetime.now()
            cancelled = False
            deadline_hit = False
            strava_limited = False
            try:
                for txt_dest in txt_destinos:
                    results = process_whatsapp_txt(
                        # O RANKING ATRAVESSA A TRAVA (27/08/2026). Este mesmo
                        # runner serve o TXT que a automacao manda as 05:30 e
                        # 15:30 (prioridade='ranking') e o que um admin sobe a
                        # mao. So o primeiro passa por cima do teto.
                        respeitar_teto=(params.get('prioridade') != 'ranking'),
                        txt_path=txt_dest,
                        cidade=cidade,
                        zip_filename=zip_filename,
                        processed_file_id=pf_id,
                        process_current_month=process_cm,
                        admin_id=admin_id,
                        progress_callback=_progress,
                        cancel_check=lambda: _is_cancelled(job_id),
                        periodos=periodos,
                        throttle_profile=throttle_profile,
                    )
                    total_links     += results.get('total_links_found', 0)
                    total_processed += results.get('activities_processed', 0)
                    total_skipped   += results.get('activities_skipped', 0)
                    total_links_mes += results.get('links_encontrados_mes', 0)
                    total_pendentes += results.get('pendentes', 0)
                    total_links_scrapeados += results.get('links_scrapeados', 0)
                    total_pend_retry += results.get('pendencias_retentaveis', 0)
                    total_pend_perm  += results.get('pendencias_permanentes', 0)
                    total_shortlinks_resolvidos += results.get('shortlinks_resolvidos', 0)
                    pending_details.extend(results.get('pending_details') or [])
                    cancelled        = results.get('cancelled', False)
                    deadline_hit     = deadline_hit or results.get('deadline_hit', False)
                    strava_limited   = strava_limited or results.get('rate_limited', False)
                    if cancelled:
                        break
            except Exception as e:
                logger.error(f'Erro em background_process: {e}', exc_info=True)
                db.session.rollback()
                # As atividades ja salvas sao validas e deduplicadas -- apagar
                # seria perder trabalho real (critico nos jobs longos de
                # "todos os periodos"). Ficam no banco; reenviar o arquivo
                # continua de onde parou gracas a deduplicacao + cache.
                try:
                    mantidas = len(_activity_ids_for_upload(pf_id, zip_filename, cidade))
                except Exception:
                    mantidas = 0
                _invalidar_consolidacao(cidade)
                _update_processed_file(
                    zip_filename, cidade, 0, mantidas, 0, 0, 0, 'erro',
                    f'{e} ({mantidas} atividades ja salvas foram mantidas; '
                    f'reenvie o arquivo para continuar)')
                _set_progress(
                    job_id, 'error',
                    f'Erro no processamento: {e} ({mantidas} atividades ja salvas '
                    f'foram mantidas; reenvie o arquivo para continuar de onde parou)',
                    0)
                _expire_job(job_id, 24 * 60 * 60)
                return

            duration = (datetime.now() - start_time).total_seconds()

            if cancelled:
                flag = _consume_cancel_flag(job_id)
                if flag.get('delete', False):
                    try:
                        deleted = _delete_upload_activities(pf_id, zip_filename, cidade)
                        db.session.commit()
                        logger.info(f"Cancelado + {deleted} atividades deletadas de {zip_filename}")
                        _update_processed_file(zip_filename, cidade, 0, 0, 0, 0, 0,
                                               'cancelado', 'Cancelado pelo usuario (atividades removidas)')
                        _set_progress(job_id, 'cancelled',
                                      f'Processamento cancelado. {deleted} atividades removidas do banco.',
                                      0)
                    except Exception as e:
                        db.session.rollback()
                        logger.error(f"Erro ao deletar apos cancel: {e}")
                        _set_progress(job_id, 'cancelled',
                                      f'Cancelado, mas erro ao remover atividades: {e}', 0)
                else:
                    _update_processed_file(zip_filename, cidade,
                                           total_links, total_processed, total_skipped,
                                           total_links_mes, total_pendentes,
                                           'cancelado', 'Cancelado pelo usuario')
                    _set_progress(job_id, 'cancelled',
                                  f'Processamento cancelado. {total_processed} atividades ja salvas foram mantidas.',
                                  0)
                _invalidar_consolidacao(cidade)
                _expire_job(job_id, 24 * 60 * 60)
                return

            # Continuacao automatica: rodada interrompida por teto/bloqueio COM
            # progresso real agenda sozinha a proxima rodada do mesmo arquivo.
            continuar = False
            cooldown = 0
            rodada_atual = int(params.get('continuation') or 0)
            progresso_real = (total_processed > 0 or total_shortlinks_resolvidos > 0)
            if ((deadline_hit or strava_limited) and progresso_real
                    and rodada_atual < _CONTINUATION_MAX_ROUNDS):
                cooldown = (_CONTINUATION_COOLDOWN_RATE_LIMIT if strava_limited
                            else _CONTINUATION_COOLDOWN_DEADLINE)
                continuar = _schedule_continuation(job_id, params,
                                                   rodada_atual + 1, cooldown)

            # O STRAVA BARROU O RANKING? ISSO SE AVISA (27/08/2026, regra do
            # dono). Nossa trava nao barra o ranking; se mesmo assim ele parou
            # por limite, o bloqueio e' do Strava — e aí o desfecho aceitavel e'
            # "o ranking nao saiu", nunca "ninguem soube".
            if strava_limited and params.get('prioridade') == 'ranking':
                try:
                    from app import alertas
                    from app.ratepacer import SharedPacer
                    try:
                        gasto = SharedPacer('strava').uso()
                    except Exception:
                        gasto = {}
                    alertas.registrar_falha(
                        cidade, 'Coleta',
                        'O Strava limitou o acesso durante o processamento do '
                        'ranking. O ranking pode nao ter saido completo.',
                        linhas_log=[
                            'Links visitados nesta rodada: %d' % total_links_scrapeados,
                            'Atividades salvas: %d' % total_processed,
                            'Pendencias: %d retentavel(is), %d permanente(s)'
                            % (total_pend_retry, total_pend_perm),
                            'Nosso proprio contador no dia: %s' % (gasto or 'sem leitura'),
                            'A trava interna NAO barra o ranking — este bloqueio '
                            'veio do Strava.',
                            'Continuacao automatica agendada: %s' % ('sim' if continuar else 'nao'),
                        ],
                        origem='upload_txt/%s' % job_id)
                except Exception as exc:
                    logger.error('Nao consegui avisar do bloqueio do Strava: %s', exc)

            if continuar:
                status = 'parcial'
            else:
                status = 'sucesso' if total_processed > 0 else 'vazio'
            _update_processed_file(zip_filename, cidade,
                                   total_links, total_processed, total_skipped,
                                   total_links_mes, total_pendentes,
                                   status, '', duration,
                                   total_links_scrapeados, pending_details)
            if total_processed > 0:
                periodos_salvos = _periodos_das_atividades(cidade, pf_id)
                _invalidar_consolidacao(cidade, periodos_salvos or periodos)
            _cleanup_old_txts(txt_folder)

            total_pend = total_pend_retry + total_pend_perm
            if total_pend > 0:
                pend_msg = (
                    f' {total_pend} pendencia(s): {total_pend_retry} retentavel(is) '
                    f'(tente reenviar depois) e {total_pend_perm} permanente(s).'
                )
            else:
                pend_msg = ''

            if total_processed > 0:
                done_msg = (
                    f'Concluido! {total_processed} atividades salvas no ranking. '
                    f'({total_skipped} duplicatas ignoradas, {total_links_scrapeados} links visitados).'
                    f'{pend_msg}'
                )
            elif total_skipped > 0 and total_pend == 0:
                # Achou links, mas TODOS ja estavam no ranking. Caso classico de
                # reenviar a mesma conversa exportada (ou uma exportacao antiga).
                done_msg = (
                    f'Nada novo para salvar: os {total_skipped} links deste periodo '
                    f'ja estavam no ranking. Se voce esperava atividades novas, '
                    f'confira se exportou uma conversa ATUALIZADA do WhatsApp '
                    f'(este arquivo so contem links que ja foram importados) ou use '
                    f'"Processar links avulsos" para adicionar links especificos.'
                    f'{pend_msg}'
                )
            elif total_links == 0:
                done_msg = (
                    'Nenhum link do Strava foi encontrado no arquivo. '
                    'Confira se exportou a conversa "Com midia".'
                )
            else:
                done_msg = (
                    f'Processamento concluido, mas nenhuma atividade entrou no ranking. '
                    f'({total_skipped} ja existiam; {total_links_mes} links do periodo analisados).'
                    f'{pend_msg}'
                )

            if continuar:
                minutos = max(1, int(cooldown // 60))
                done_msg += (
                    f' O arquivo ainda tem links a processar: a proxima rodada '
                    f'(numero {rodada_atual + 1}) comeca sozinha em ~{minutos} min. '
                    f'Pode fechar o navegador.'
                )

            _set_progress(job_id, 'done', done_msg, 100)
            _expire_job(job_id, 24 * 60 * 60)
            logger.info(
                f"Background concluido: {total_links} links, "
                f"{total_processed} salvas, {total_skipped} ignoradas, "
                f"{duration:.1f}s"
                + (f" (continuacao {rodada_atual + 1} agendada)" if continuar else "")
            )
    finally:
        _LOCAL_THREADS.pop(job_id, None)
        _delete_resume_claim(job_id)


def _process_saved_zip(zip_path, zip_filename, cidade, admin_id, process_cm, job_id,
                       periodos=None, throttle_profile=None, prioridade=None):
    """
    Processa um ZIP ja salvo em disco.
    Lanca o background thread e retorna JSON com job_id para polling.
    """
    txt_folder  = current_app.config['UPLOAD_FOLDER_TXT']
    temp_folder = os.path.join(
        current_app.config['TEMP_EXTRACT_FOLDER'],
        'extract',
        _storage_slug(job_id, 'job'),
    )
    os.makedirs(temp_folder, exist_ok=True)
    _ensure_upload_schema()

    _set_progress(job_id, 'upload',
                  'Calculando hash do arquivo para verificar duplicatas...', 25,
                  zip_filename, cidade)

    file_hash = _file_hash(zip_path)
    tipo_periodo  = throttle_profile or ('mes_atual' if process_cm else 'todos_periodos')
    valor_periodo = _valor_periodo_upload(periodos)
    _recover_stuck_uploads()
    existing = ProcessedFile.query.filter_by(
        hash_arquivo=file_hash, cidade=cidade,
        tipo_periodo=tipo_periodo, valor_periodo=valor_periodo,
    ).filter(~ProcessedFile.status.in_(('erro', 'parcial'))).first()
    if existing:
        os.remove(zip_path)
        _expire_job(job_id, 60)
        if existing.status == 'processando':
            existing_job_id = _extract_job_id_from_message(existing.mensagem_erro)
            existing_job = _read_progress_file(existing_job_id) if existing_job_id else None
            if _is_stale_job(existing_job):
                return jsonify(
                    status='warning',
                    message=(
                        'Este arquivo tem um processamento anterior sem resposta. '
                        'Cancele o processamento pendente mantendo ou removendo as '
                        'atividades parciais antes de reenviar.'
                    ),
                ), 200
            return jsonify(
                status='warning',
                message='Este arquivo ainda esta em processamento nesta cidade para este periodo.',
            ), 200
        return jsonify(
            status='warning',
            message='Este arquivo ja foi processado anteriormente nesta cidade para este periodo.',
        ), 200
    # Apagar registro de erro/parcial anterior para este hash+periodo (recriado)
    ProcessedFile.query.filter_by(
        hash_arquivo=file_hash, cidade=cidade,
        tipo_periodo=tipo_periodo, valor_periodo=valor_periodo,
    ).delete()
    db.session.commit()
    # Reenvio manual assume o lugar de qualquer continuacao agendada da cidade
    # (evita duas rodadas simultaneas dobrando requisicoes ao Strava).
    _cancel_city_continuations(cidade)

    _set_progress(job_id, 'extract', 'Extraindo conversa do WhatsApp do ZIP...', 35,
                  zip_filename, cidade)
    _cleanup_temp(temp_folder)

    txt_paths = []
    ext = os.path.splitext(zip_filename)[1].lower()
    if ext == '.txt' or (not zipfile.is_zipfile(zip_path) and _looks_like_text_file(zip_path)):
        txt_paths.append(zip_path)
    else:
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for idx, member in enumerate(zf.namelist()):
                    if _is_wa_txt(os.path.basename(member)):
                        safe_out = os.path.join(temp_folder, f'chat_export_{idx}.txt')
                        with open(safe_out, 'wb') as out_f:
                            out_f.write(zf.read(member))
                        txt_paths.append(safe_out)
        except zipfile.BadZipFile:
            os.remove(zip_path)
            _set_progress(job_id, 'error', 'Arquivo ZIP invalido/corrompido ou TXT sem conteudo legivel.', 0)
            _expire_job(job_id, 60)
            return jsonify(status='error', message='Arquivo ZIP invalido/corrompido ou TXT sem conteudo legivel.'), 400
        except Exception as e:
            logger.error(f"Erro ao extrair ZIP: {e}")
            os.remove(zip_path)
            _set_progress(job_id, 'error', f'Erro ao extrair ZIP: {e}', 0)
            _expire_job(job_id, 60)
            return jsonify(status='error', message=f'Erro ao extrair ZIP: {e}'), 500

    if not txt_paths:
        os.remove(zip_path)
        _cleanup_temp(temp_folder)
        _expire_job(job_id, 60)
        return jsonify(
            status='error',
            message=(
                'Nenhum arquivo de conversa (.txt) encontrado no ZIP. '
                'Verifique se o arquivo e uma exportacao do WhatsApp.'
            ),
        ), 400

    timestamp   = datetime.now().strftime('%Y%m%d_%H%M%S')
    cidade_slug = _storage_slug(cidade, 'cidade')
    txt_destinos = []
    job_slug = _storage_slug(str(job_id)[:8], 'job')
    for src_path in txt_paths:
        txt_safe = secure_filename(os.path.basename(src_path)) or 'conversa.txt'
        txt_dest = os.path.join(txt_folder, f"{timestamp}_{cidade_slug}_{job_slug}_{txt_safe}")
        shutil.copy2(src_path, txt_dest)
        txt_destinos.append(txt_dest)

    _cleanup_temp(temp_folder)

    try:
        os.remove(zip_path)
        logger.info(f"ZIP removido apos extracao: {zip_filename}")
    except Exception as e:
        logger.warning(f"Nao foi possivel deletar ZIP: {e}")

    _set_progress(job_id, 'reading', 'Preparando para ler as mensagens...', 38,
                  zip_filename, cidade)

    try:
        db.session.add(ProcessedFile(
            arquivo_nome=zip_filename,
            cidade=cidade,
            hash_arquivo=file_hash,
            tipo_periodo=tipo_periodo,
            valor_periodo=valor_periodo,
            total_atividades=0,
            atividades_validas=0,
            processado_em=datetime.now(),
            processado_por=admin_id,
            tempo_processamento=0,
            status='processando',
            mensagem_erro=f'job_id={job_id}',
        ))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Erro ao salvar ProcessedFile inicial: {e}", exc_info=True)
        for txt_dest in txt_destinos:
            try:
                os.remove(txt_dest)
            except OSError:
                pass
        _set_progress(job_id, 'error', f'Nao foi possivel registrar o processamento no banco: {e}', 0)
        _expire_job(job_id, 24 * 60 * 60)
        return jsonify(
            status='error',
            message='Nao foi possivel registrar o processamento no banco. Tente novamente apos verificar o banco.',
        ), 500

    # Obter pf_id para rastrear atividades deste processamento
    pf_obj = ProcessedFile.query.filter_by(
        hash_arquivo=file_hash, cidade=cidade,
        tipo_periodo=tipo_periodo, valor_periodo=valor_periodo,
    ).first()
    pf_id  = pf_obj.id if pf_obj else None
    if not pf_id:
        for txt_dest in txt_destinos:
            try:
                os.remove(txt_dest)
            except OSError:
                pass
        _set_progress(job_id, 'error', 'Processamento sem registro inicial no banco.', 0)
        _expire_job(job_id, 24 * 60 * 60)
        return jsonify(
            status='error',
            message='Processamento sem registro inicial no banco. Tente novamente.',
        ), 500

    app = current_app._get_current_object()

    job_params = {
        'kind': 'upload_txt',
        # A FINALIDADE VIAJA COM O TRABALHO (27/08/2026). 'ranking' = veio da
        # automacao (as 05:30/15:30) e atravessa a trava de volume; qualquer
        # outra coisa e' upload humano e obedece.
        'prioridade': prioridade,
        'txt_paths': txt_destinos,
        'zip_filename': zip_filename,
        'cidade': cidade,
        'process_cm': bool(process_cm),
        'periodos': [list(p) for p in sorted(periodos)] if periodos else None,
        'admin_id': admin_id,
        'pf_id': pf_id,
        'throttle_profile': throttle_profile,
        'owner_pid': os.getpid(),
    }
    _set_job_params(job_id, job_params)

    t = threading.Thread(target=_run_upload_job, args=(app, job_id, job_params), daemon=True)
    _LOCAL_THREADS[job_id] = t
    t.start()
    logger.info(f"Background thread iniciada para {zip_filename} (job={job_id})")

    return jsonify(
        status='success',
        job_id=job_id,
        message='Arquivo recebido! Processando em segundo plano...',
        ranking_url=url_for('ranking.current'),
    ), 200


def _meses_participacao_grupo(cidade, athlete_id):
    """(ano, mes) em que o atleta teve pelo menos um link postado no grupo.

    So conta atividade cuja origem foi TXT/link do grupo: importacoes por
    perfil e ajustes manuais nao valem como participacao. E o piso que impede
    a importacao de encher o ranking com meses em que o atleta nao participava.
    """
    linhas = (
        db.session.query(Activity.data_hora)
        .filter(
            Activity.cidade == cidade,
            Activity.id_usuario_strava == str(athlete_id),
            Activity.data_hora.isnot(None),
            Activity.arquivo_origem.isnot(None),
            ~Activity.arquivo_origem.like('importacao_atleta_%'),
            Activity.arquivo_origem != 'ajuste_manual',
        )
        .all()
    )
    return {(d.year, d.month) for (d,) in linhas}


def _todos_meses_ate_hoje(inicio):
    """Lista (ano, mes) de ``inicio`` ate o mes PASSADO, em ordem cronologica.

    O mes vigente fica de fora de proposito (dono, 22/08/2026): ele ainda
    esta aberto, se sustenta pelo link postado no grupo (regra 4-A), nao por
    scraping direto do perfil. Foi assim que um atleta (ja saiu do grupo) e uma
    atleta (nao posta link) entraram no ranking de agosto/2026 com km
    que vieram so do perfil dela, nao de link nenhum. Quem quiser o mes atual
    de proposito usa "periodo especifico".
    """
    hoje = datetime.now()
    ano, mes = inicio
    saida = []
    while (ano, mes) < (hoje.year, hoje.month):
        saida.append((ano, mes))
        mes += 1
        if mes > 12:
            mes = 1
            ano += 1
    return saida


def _piso_importacao_cidade(cidade):
    """(ano, mes) de fundacao do grupo desta cidade, ou None se o master/admin
    nao cadastrou (Painel > Cidades > Editar). Sem integracao com o WAHA --
    e um dado que so o dono da cidade sabe, preenchido a mao."""
    cfg = Config.query.filter_by(cidade=cidade).first()
    if not cfg or not cfg.piso_importacao_ano or not cfg.piso_importacao_mes:
        return None
    return (int(cfg.piso_importacao_ano), int(cfg.piso_importacao_mes))


def _periodos_respeitando_participacao(cidade, athlete_id, periodos,
                                       ignorar_participacao=False):
    """Filtra os periodos pedidos pelos meses em que o atleta postou no grupo.

    ignorar_participacao=True pula essa checagem e usa direto o piso de
    fundacao do grupo desta cidade -- para o atleta que trocou de perfil
    Strava e cujo perfil NOVO nunca postou link nenhum, mas que participa do
    grupo desde antes.

    Retorna (periodos_efetivos, aviso). Levanta RuntimeError com mensagem
    amigavel quando nao sobra nenhum mes importavel.
    """
    piso_cidade = _piso_importacao_cidade(cidade)
    if ignorar_participacao:
        if not piso_cidade:
            raise RuntimeError(
                'Esta cidade ainda nao tem a fundacao do grupo cadastrada '
                '(Painel > Cidades > Editar). Cadastre o mes/ano antes de usar '
                'esta opcao, ou desmarque-a para usar o piso normal (primeiro '
                'link deste atleta no grupo).'
            )
        primeiro = piso_cidade
    else:
        participacao = _meses_participacao_grupo(cidade, athlete_id)
        if not participacao:
            raise RuntimeError(
                'Este atleta ainda nao tem nenhum link postado no grupo desta '
                'cidade. A importacao por perfil parte do primeiro link do atleta '
                'no grupo: processe primeiro um TXT do WhatsApp (ou um link avulso) '
                'com alguma atividade dele -- ou marque a opcao de importar desde '
                'a fundacao do grupo, se o atleta trocou de perfil Strava.'
            )
        # O que importa e o PISO: o mes do primeiro link do atleta no grupo, mas
        # nunca antes da fundacao do grupo da cidade (dado no banco pode estar
        # errado -- cidades ja foram renomeadas/misturadas). Do piso para frente
        # a importacao e livre -- e uma acao deliberada do admin para trazer o
        # historico do atleta.
        primeiro = max(min(participacao), piso_cidade) if piso_cidade else min(participacao)
    candidatos = sorted(periodos) if periodos else _todos_meses_ate_hoje(primeiro)
    efetivos = [p for p in candidatos if p >= primeiro]
    if not efetivos:
        agora = datetime.now()
        if not periodos and primeiro == (agora.year, agora.month):
            # "Todos os periodos" sempre exclui o mes vigente (Fase 2,
            # 22/08/2026) -- se o piso E o mes vigente, nao sobra nada para
            # "todos os periodos" varrer, e o erro generico abaixo ia soar
            # como se o piso estivesse errado, quando a causa real e outra.
            raise RuntimeError(
                f'O piso desta importacao e o mes vigente ({primeiro[1]:02d}/'
                f'{primeiro[0]}), que "todos os periodos" sempre exclui (ele '
                f'ainda esta aberto -- conta pelo link postado no grupo, nao '
                f'por importacao de perfil). Use "periodo especifico" para '
                f'importar so o mes atual, se for mesmo necessario.'
            )
        raise RuntimeError(
            f'O periodo escolhido e anterior ao piso desta importacao '
            f'({primeiro[1]:02d}/{primeiro[0]}). Escolha esse mes ou um posterior.'
        )
    ignorados = len(candidatos) - len(efetivos)
    aviso = ''
    if ignorados:
        aviso = (
            f'Piso desta importacao: {primeiro[1]:02d}/{primeiro[0]}: '
            f'{ignorados} mes(es) anterior(es) foram ignorados.'
        )
    return set(efetivos), aviso


def _periodos_para_json(periodos):
    """set{(ano, mes)} -> lista serializavel. `None` (todos os periodos) fica None."""
    if not periodos:
        return None
    return [[int(ano), int(mes)] for ano, mes in sorted(periodos)]


def _periodos_de_json(valor):
    """O caminho de volta de `_periodos_para_json`."""
    if not valor:
        return None
    return {(int(par[0]), int(par[1])) for par in valor if len(par) == 2}


_RAIZ_PROJETO = Path(__file__).resolve().parents[2]
_LOG_JOBRUNNER = _RAIZ_PROJETO / 'logs' / 'jobrunner.log'
_LOG_JOBRUNNER_MAX = 1024 * 1024


def _python_do_projeto():
    """O interpretador que roda o processo de trabalho longo.

    NAO da para usar `sys.executable` direto: dentro do uWSGI ele aponta para o
    binario do uwsgi, nao para um Python. O caminho bom e' o venv do projeto,
    que e' o mesmo que o `wsgi.py` ativa no boot.
    """
    candidatos = [
        _RAIZ_PROJETO / 'venv' / 'bin' / 'python',        # Linux (producao)
        _RAIZ_PROJETO / 'venv' / 'Scripts' / 'python.exe',  # Windows (banco de provas)
    ]
    exe = getattr(sys, 'executable', '') or ''
    if 'python' in os.path.basename(exe).lower():
        candidatos.append(Path(exe))
    for c in candidatos:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def _run_atualizar_perfis(app, job_id, params):
    """Adaptador: a fila chama (app, job_id, params); o trabalho mora no admin.

    Ele fica la porque e' de la que vem tudo que ele toca (Atleta, o cofre, a
    trava do nome repetido). Aqui esta so' a tomada.
    """
    from app.blueprints.admin import _run_atualizar_fotos_job
    _run_atualizar_fotos_job(app, job_id,
                             params.get('cidade') or '',
                             params.get('o_que') or 'foto',
                             params.get('sids'),
                             bool(params.get('automatico')))


def enfileirar_atualizacao_de_perfis(cidade, o_que, sids=None, admin_id=None,
                                     automatico=False):
    """Poe a atualizacao de nomes/fotos NA FILA, junto com todo o resto.

    POR QUE AQUI, E NAO NUMA THREAD PROPRIA (07/09/2026, pedido do dono).
    Ate hoje este trabalho corria por fora: thread solta, registro so na
    memoria, invisivel na tela de upload. Resultado -- em 07/09 dois deles
    rodaram em cima de uma importacao e as tres coisas andaram a um terco da
    velocidade, sem que nada na tela dissesse por que. A fila ja resolve isso
    de graca: a vaga e' UMA, entao so ha uma requisicao por vez; o trabalho
    aparece na lista com o que esta fazendo; e ninguem precisa de regra de
    prioridade, porque ninguem disputa.

    E ainda ganha o processo proprio (`app.jobrunner`), que sobrevive ao
    reload do site -- a thread antiga morria em todo `.py` salvo e deixava um
    registro "rodando" para sempre.
    """
    job_id = str(uuid.uuid4())
    params = {
        'kind':       'atualizar_perfis',
        'cidade':     cidade,
        'o_que':      o_que,
        'sids':       [str(s) for s in sids] if sids else None,
        'admin_id':   admin_id,
        'automatico': bool(automatico),
        'owner_pid':  os.getpid(),
    }
    rotulo = 'nomes' if o_que == 'nome' else 'fotos'
    origem = 'atualizar_perfis'

    if not _ha_vaga_para_trabalho_pesado():
        _set_progress(
            job_id, _ESTAGIO_NA_FILA,
            'Na fila: a atualização de %s começa sozinha assim que o trabalho '
            'em andamento terminar — pode fechar a página.' % rotulo,
            3, origem, cidade,
        )
        _set_job_params(job_id, params)
        logger.info('Entrou na fila: atualizacao de %s em %s', rotulo, cidade)
        return job_id

    _set_progress(job_id, 'reading',
                  'Começando a atualização de %s...' % rotulo, 5, origem, cidade)
    _set_job_params(job_id, params)
    if _lancar_job_em_processo(job_id, params) is None:
        app_atual = current_app._get_current_object()
        t = threading.Thread(target=_run_atualizar_perfis,
                             args=(app_atual, job_id, params), daemon=True)
        _LOCAL_THREADS[job_id] = t
        t.start()
    return job_id


def _lancar_job_em_processo(job_id, params):
    """Roda o trabalho longo FORA do processo do site. Devolve o PID, ou None.

    POR QUE (26/08/2026, `ERROS.md` §4.24 e `app/jobrunner.py`)
    Thread dentro do worker web morre quando o worker e' reciclado, nao pode ser
    cancelada de verdade e deixa a memoria presa la. Processo proprio resolve os
    tres.

    SE NAO DER, NAO QUEBRA: devolver None faz o chamador usar a thread de
    sempre. Uma importacao rodando do jeito antigo e' muito melhor que uma
    importacao que nao comeca -- a novidade nao pode ser um ponto de falha novo.
    """
    python = _python_do_projeto()
    if not python:
        logger.warning('Trabalho longo %s: nao achei o Python do projeto; '
                       'seguindo pela thread de sempre.', job_id)
        return None
    try:
        ambiente = dict(os.environ)
        ambiente['MOVIMENTE_SO_JOB'] = '1'
        ambiente['PYTHONPATH'] = str(_RAIZ_PROJETO)
        ambiente['PYTHONIOENCODING'] = 'utf8'
        # A SAIDA DO PROCESSO NAO PODE IR PARA O VAZIO (achado no teste de
        # 26/08): com `DEVNULL`, um erro de ARRANQUE -- permissao no cofre, no
        # log, dependencia faltando -- morre calado, e a tela fica girando sem
        # ninguem saber por que. Vai para um arquivo proprio, que a faxina
        # diaria corta quando passa de 1 MB.
        saida = open(_LOG_JOBRUNNER, 'a', encoding='utf-8', errors='replace')
        processo = subprocess.Popen(
            [python, '-m', 'app.jobrunner', job_id],
            cwd=str(_RAIZ_PROJETO),
            env=ambiente,
            stdin=subprocess.DEVNULL,
            stdout=saida,
            stderr=subprocess.STDOUT,
            start_new_session=True,   # sobrevive ao reload do worker que o criou
        )
        saida.close()   # o filho ficou com o descritor; este lado nao precisa mais
        # GUARDA A REFERENCIA PARA PODER RECOLHER O PROCESSO DEPOIS. Sem isso o
        # filho terminado vira zumbi ate o worker que o criou morrer -- e zumbi
        # engana quem pergunta se ele ainda esta vivo (ver `_processo_vivo`).
        # Quem recolhe e' a faxina, com `poll()`.
        _PROCESSOS_FILHOS[job_id] = processo
    except Exception as exc:
        logger.warning('Trabalho longo %s: nao consegui iniciar o processo (%s); '
                       'seguindo pela thread de sempre.', job_id, exc)
        return None

    params = dict(params)
    params['owner_pid'] = processo.pid
    params['processo_proprio'] = True
    _set_job_params(job_id, params)
    logger.info('Trabalho longo %s iniciado no processo %s (fora do site)',
                job_id, processo.pid)
    return processo.pid


_PRAZO_PARADA_LIMPA = 30


def _processo_vivo(pid):
    """O processo `pid` esta MESMO trabalhando?

    CUIDADO COM O ZUMBI (medido em 26/08/2026, no teste da parada forcada):
    `os.kill(pid, 0)` continua achando um processo que JA MORREU mas ainda nao
    foi recolhido pelo pai -- e o pai aqui e' o worker do uWSGI, que nunca faz
    `wait()`. Sem esta checagem o SIGKILL "nao funcionava": o processo morria e
    continuava sendo dado como vivo, para sempre. Pior, `_maybe_resume_job`
    decide se um trabalho morreu por este mesmo caminho -- um zumbi faria ele
    concluir "o dono esta de pe" e NUNCA retomar o trabalho.
    """
    try:
        os.kill(int(pid), 0)
    except (OSError, ProcessLookupError, TypeError, ValueError):
        return False
    try:
        # No Linux o estado vem depois do nome do processo, que pode conter
        # parenteses -- por isso corta no ULTIMO ')'.
        with open('/proc/%d/stat' % int(pid), 'r') as fh:
            estado = fh.read().rsplit(')', 1)[1].split()[0]
        return estado != 'Z'
    except (OSError, IndexError, ValueError):
        return True     # sem /proc (Windows, banco de provas): vale o kill(0)


def _garantir_parada(job_id, job, prazo=_PRAZO_PARADA_LIMPA):
    """Cancelar vira PARADA DE FATO quando o trabalho tem processo proprio.

    A ORDEM IMPORTA, e e' o oposto de "matar logo":
      1. o arquivo `.cancel` ja foi gravado -- o processo o ve entre uma espera
         e outra e sai sozinho, gravando onde parou. Este e' o desfecho bom.
      2. passado o prazo, se ainda estiver de pe, SIGTERM (ele ainda pode
         fechar o que estava fazendo).
      3. so entao SIGKILL.

    POR QUE ESPERAR ANTES DE MATAR: cada atividade lida ja foi gravada no banco
    na hora (commit por atividade), entao matar nao perde km -- mas perde o
    CHECKPOINT do mes em andamento, e a proxima retomada refaz aquele mes
    inteiro no Strava. Trinta segundos de espera custam menos que isso.

    Roda numa thread curta: a tela responde na hora, sem esperar o prazo.
    """
    params = (job or {}).get('params') or {}
    pid = params.get('owner_pid')
    if not params.get('processo_proprio') or not pid:
        return          # job em thread: quem para e' o proprio codigo, como antes
    if int(pid) == os.getpid():
        return          # sou eu mesmo; nao ha processo separado para parar

    def _esperar_e_parar():
        import signal
        fim = _time.time() + prazo
        while _time.time() < fim:
            if not _processo_vivo(pid):
                logger.info('Trabalho %s parou sozinho apos o pedido de cancelar', job_id)
                return
            _time.sleep(2)
        for sinal, nome in ((getattr(signal, 'SIGTERM', 15), 'SIGTERM'),
                            (getattr(signal, 'SIGKILL', 9), 'SIGKILL')):
            if not _processo_vivo(pid):
                return
            try:
                os.kill(int(pid), sinal)
                logger.warning('Trabalho %s nao parou em %ss: %s enviado ao processo %s',
                               job_id, prazo, nome, pid)
            except Exception as exc:
                logger.warning('Nao consegui enviar %s ao processo %s: %s', nome, pid, exc)
                return
            _time.sleep(5)

    threading.Thread(target=_esperar_e_parar, daemon=True,
                     name='parar-%s' % job_id[:8]).start()


def _iniciar_importacao_atleta(source_url, cidade, admin_id, periodos,
                               throttle_profile=None, ignorar_participacao=False):
    """Inicia scraping historico de um perfil (ou de uma atividade do perfil).

    A listagem das atividades do perfil usa o cookie de sessao do Strava
    guardado no cofre (o Strava nao serve mais essa lista publicamente); ja a
    leitura de cada atividade continua sendo publica, sem cookie.

    OS PARAMETROS FICAM GRAVADOS EM DISCO (14/08/2026). Sem isso a importacao
    morria em toda reinicializacao do worker — e o worker reinicia sozinho a
    cada arquivo salvo (`py-autoreload`). Em 14/08 havia NOVE importacoes
    paradas em `scraping`, sete delas de 9 a 14 horas antes, e nenhuma tinha
    como voltar: `_maybe_resume_job` so sabia retomar `upload_txt`.
    """
    job_id = str(uuid.uuid4())
    params = {
        'kind':             'importacao_atleta',
        'source_url':       source_url,
        'cidade':           cidade,
        'admin_id':         admin_id,
        'periodos':         _periodos_para_json(periodos),
        'throttle_profile': throttle_profile,
        'ignorar_participacao': bool(ignorar_participacao),
        'owner_pid': os.getpid(),
    }
    origem = f'importacao_atleta_{job_id}'

    # SEM VAGA NAO E' RECUSA, E' FILA (26/08/2026, pedido do dono).
    # O trabalho e' criado do mesmo jeito e fica esperando; o ticker de fundo o
    # inicia sozinho quando um dos que estao rodando terminar. Ninguem precisa
    # voltar na tela nem clicar de novo.
    if not _ha_vaga_para_trabalho_pesado():
        _set_progress(
            job_id, _ESTAGIO_NA_FILA,
            'Na fila: o servidor ja esta com %d importacao(oes) em andamento. '
            'Esta comeca sozinha assim que uma delas terminar — pode fechar a '
            'pagina.' % _LIMITE_TRABALHOS_PESADOS,
            3, origem, cidade,
        )
        _set_job_params(job_id, params)
        # UMA LINHA, COM O NOME (27/08/2026, pedido do dono): quando um
        # segundo atleta entra, o unico recado util e' de quem ele e'.
        logger.info('Entrou na fila: %s (servidor com %d trabalho(s) pesado(s))',
                    _nome_do_atleta(_id_do_trabalho(params)) or 'atleta ainda nao identificado',
                    _trabalhos_pesados_rodando())
        return job_id

    _set_progress(job_id, 'reading', 'Preparando importacao do atleta...', 5,
                  origem, cidade)
    _set_job_params(job_id, params)

    # PRIMEIRO caminho: processo proprio, fora do site (26/08/2026).
    if _lancar_job_em_processo(job_id, params) is not None:
        return job_id

    # RESERVA: do jeito de sempre. Nao conseguir o processo separado nao pode
    # impedir a importacao de comecar.
    app = current_app._get_current_object()
    thread = threading.Thread(target=_run_importacao_atleta,
                              args=(app, job_id, params), daemon=True)
    _LOCAL_THREADS[job_id] = thread
    thread.start()
    return job_id


def _listar_perfil_com_checkpoint(job_id, params, profile_url, meses,
                                  session_cookie, throttle, progresso):
    """Lista as atividades do perfil MES A MES, gravando o que já leu.

    Devolve (athlete_id, urls) como a chamada única devolvia.

    POR QUE MES A MES
    Um histórico de 62 meses levava uma hora só para ser LISTADO, e nada era
    gravado antes de a lista terminar. Qualquer reinício no meio jogava tudo
    fora e recomeçava do primeiro mês — o dono viu isso rodar em círculo a
    tarde inteira sem uma atividade salva.

    Aqui cada mês lido vai para o arquivo do job. Retomar continua de onde
    parou: o trabalho anda, e nenhuma requisição ao Strava é repetida à toa
    (regra 4-C, a fila de acesso ao Strava é recurso limitado).
    """
    lidos = {tuple(p) for p in (params.get('meses_lidos') or []) if len(p) == 2}
    urls = list(params.get('urls_encontradas') or [])
    athlete_id = params.get('athlete_id')
    total = len(meses)

    for indice, (ano, mes) in enumerate(meses, 1):
        if (ano, mes) in lidos:
            continue
        if _is_cancelled(job_id):
            break
        progresso('scraping',
                  'Lendo o perfil no Strava: %02d/%d (%d de %d)...'
                  % (mes, ano, indice, total),
                  15 + int(20 * indice / max(total, 1)))
        achado, do_mes = extract_strava_profile_activity_urls(
            profile_url,
            periodos={(ano, mes)},
            session_cookie=session_cookie,
            throttle=throttle,
        )
        if achado:
            athlete_id = achado
        for u in (do_mes or []):
            if u not in urls:
                urls.append(u)
        lidos.add((ano, mes))

        # O CHECKPOINT. Guardar depois de cada mês é o que faz retomar
        # significar "continuar" em vez de "recomeçar".
        params['meses_lidos'] = [[a, m] for a, m in sorted(lidos)]
        params['urls_encontradas'] = urls
        params['athlete_id'] = athlete_id
        _set_job_params(job_id, params)

    progresso('reading',
              '%d mês(es) lidos, %d atividade(s) na lista. Validando pelo '
              'Strava...' % (len(lidos), len(urls)), 35)
    return athlete_id, urls


def _run_recuperacao_links(app, job_id, params):
    """Reprocessa os links que uma importacao anterior nao conseguiu ler.

    POR QUE EXISTE (pedido do dono, 27/08/2026): 482 atividades ficaram para
    tras desde o inicio dos uploads, espalhadas por 9 atletas. A causa foi
    diagnosticada no mesmo dia -- a raspagem de atividade ia SEM cookie e o
    Strava devolvia HTTP 200 com a casca de login (`ERROS.md` §4.32). Com o
    cookie e os cinco leitores no ar, os mesmos links leem normalmente.

    TRES GUARDAS, e nenhuma e' opcional:
      * **o mes vigente fica de fora** -- e' do processamento do mes;
      * **so entra atividade DO ATLETA da lista** (regra 4-D): o feed de perfil
        traz treino em grupo de outras pessoas, e foi isso que gerou boa parte
        das "perdas" que nunca foram perdas;
      * **passa pelo throttle** (regra 4-C): sao centenas de links, um atras do
        outro, no mesmo IP que a rodada usa.
    """
    from app.blueprints.processing import build_strava_throttle

    cidade   = params.get('cidade')
    admin_id = params.get('admin_id')
    alvo     = str(params.get('athlete_id') or '')
    urls     = list(params.get('urls') or [])
    origem   = 'recuperacao_%s' % job_id

    with app.app_context():
        nome = _nome_do_atleta(alvo) or ('perfil %s' % alvo)
        agora = datetime.now()
        mes_vigente = {(agora.year, agora.month)}
        total = len(urls)
        feitos = dict(params.get('feitos') or {})
        conta = {'salvas': 0, 'ja_tinha': 0, 'mes_vigente': 0,
                 'de_outro': 0, 'nao_lidas': 0, 'nao_pontua': 0}
        conta.update(params.get('conta') or {})
        pendencias = list(params.get('pendencias') or [])

        def _cancelado():
            return bool(_is_cancelled(job_id))

        throttle = build_strava_throttle(mode='todos_periodos', cidade=cidade,
                                         cancel_check=_cancelado)
        _set_progress(job_id, 'reading',
                      'Recuperando %d link(s) de %s...' % (total, nome),
                      5, origem, cidade)
        logger.info('Recuperacao %s: %d link(s) de %s (%s)',
                    job_id, total, nome, alvo)

        for i, url in enumerate(urls, 1):
            if url in feitos:
                continue
            if _cancelado():
                _set_progress(job_id, 'cancelled',
                              'Recuperação cancelada pelo usuário.', 0,
                              origem, cidade)
                _expire_job(job_id, 24 * 60 * 60)
                return
            try:
                item = _process_single_strava_url(
                    url, cidade=cidade, admin_id=admin_id,
                    only_current_month=False, periodos=None,
                    origem=origem, throttle=throttle,
                    excluir_periodos=mes_vigente,
                    # NADA DE TERCEIRO ENTRA, NEM POR UM INSTANTE (27/08).
                    atleta_esperado=alvo or None,
                )
            except StravaLimiteDeVolume as limite:
                # CONGELA, NAO MORRE (27/08/2026). O teto e' NOSSO, nao do
                # Strava: nada falhou. O que ja foi feito fica em `feitos`, e
                # a volta acontece sozinha quando a janela virar.
                params['pausa_programada'] = True
                params['pausa_ate'] = _time.time() + limite.segundos
                params['pausado_em'] = _time.time()
                _set_job_params(job_id, params)
                volta = datetime.now() + timedelta(seconds=limite.segundos)
                _set_progress(
                    job_id, 'reading',
                    '%s: pausado no link %d de %d — teto de requisições ao '
                    'Strava (%s). Volta sozinho às %s.'
                    % (nome, i, total, limite.janela, volta.strftime('%H:%M')),
                    5 + int(90.0 * i / max(total, 1)), origem, cidade)
                logger.warning('Recuperacao %s pausada pelo teto (%s), volta as %s',
                               job_id, limite.janela, volta.strftime('%H:%M'))
                return
            except Exception as exc:
                conta['nao_lidas'] += 1
                pendencias.append({'url': url, 'motivo': str(exc)[:160],
                                   'etapa': 'excecao', 'repetir': True})
                logger.warning('Recuperacao %s: %s levantou %s', job_id, url, exc)
                item = None

            if item:
                situacao = item.get('status')
                if situacao == 'salvo':
                    conta['salvas'] += 1
                elif situacao == 'duplicado':
                    conta['ja_tinha'] += 1
                elif situacao == 'ignorado':
                    recado = item.get('message') or ''
                    if 'mes vigente' in recado:
                        conta['mes_vigente'] += 1
                    elif 'outra pessoa' in recado:
                        conta['de_outro'] += 1
                    else:
                        conta['nao_pontua'] = conta.get('nao_pontua', 0) + 1
                else:
                    conta['nao_lidas'] += 1
                    pendencias.append({'url': url,
                                       'motivo': (item.get('message') or '')[:160],
                                       'etapa': 'leitura', 'repetir': True})

            feitos[url] = 1
            params['feitos'] = feitos
            params['conta'] = conta
            params['pendencias'] = pendencias[:400]
            _set_job_params(job_id, params)
            _set_progress(
                job_id, 'reading',
                '%s: link %d de %d — %d recuperada(s), %d ja estavam, '
                '%d do mes vigente (ficam para o mes), %d nao lida(s).'
                % (nome, i, total, conta['salvas'], conta['ja_tinha'],
                   conta['mes_vigente'], conta['nao_lidas']),
                5 + int(90.0 * i / max(total, 1)), origem, cidade)

        # SO O QUE E' DO ATLETA (regra 4-D). Identico ao que a importacao ja
        # faz no fim: o feed traz treino em grupo de terceiro, e km de outra
        # pessoa no ranking e' pior que km faltando.
        # REDE DE SEGURANCA, NAO CAMINHO PRINCIPAL (27/08/2026). A guarda de
        # verdade agora e' antes de gravar (`atleta_esperado`); isto aqui so
        # existe para o caso de alguma passar. Se este numero for diferente de
        # zero, a guarda de cima falhou e o log tem de gritar.
        de_outros = 0
        if alvo:
            de_outros = (Activity.query
                         .filter_by(cidade=cidade, arquivo_origem=origem)
                         .filter(Activity.id_usuario_strava != alvo)
                         .delete(synchronize_session=False))
            if de_outros:
                db.session.commit()
                logger.error('Recuperacao %s: %d atividade(s) de terceiro '
                             'PASSARAM pela guarda de gravacao e foram '
                             'apagadas no fim. Isso nao devia acontecer.',
                             job_id, de_outros)
        conta['de_outro'] += de_outros
        conta['salvas'] = max(0, conta['salvas'] - de_outros)

        periodos_salvos = _periodos_das_atividades(cidade)
        if conta['salvas']:
            _invalidar_consolidacao(cidade, periodos_salvos)

        recado = ('Recuperação concluída para %s. %d atividade(s) recuperada(s). '
                  '%d já estavam no ranking. %d eram de outra pessoa (treino em '
                  'grupo). %d não pontuam (sem distância ou modalidade). '
                  '%d do mês vigente ficaram para o processamento do mês. '
                  '%d continuam sem leitura.'
                  % (nome, conta['salvas'], conta['ja_tinha'], conta['de_outro'],
                     conta.get('nao_pontua', 0), conta['mes_vigente'],
                     conta['nao_lidas']))
        params['conta'] = conta
        _set_job_params(job_id, params)
        _set_progress(job_id, 'done', recado, 100, origem, cidade)
        logger.info('Recuperacao %s: %s', job_id, recado)
        _expire_job(job_id, 24 * 60 * 60)


def _run_importacao_atleta(app, job_id, params):
    """Roda (ou RETOMA) a importacao de um perfil.

    Retomar e simplesmente rodar de novo com os mesmos parametros: a
    deduplicacao por id de atividade torna isto idempotente — o que ja foi
    salvo e ignorado e o scraping segue do ponto em que parou. E o mesmo
    raciocinio do `upload_txt`.
    """
    source_url       = params.get('source_url')
    cidade           = params.get('cidade')
    admin_id         = params.get('admin_id')
    periodos         = _periodos_de_json(params.get('periodos'))
    throttle_profile = params.get('throttle_profile')
    origem = f'importacao_atleta_{job_id}'

    def _progress(stage, message, pct):
        _set_progress(job_id, stage, message, pct, origem, cidade)

    def _background():
        with app.app_context():
            try:
                session_cookie = secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)
                profile_url = source_url
                profile_match = STRAVA_PROFILE_RE.match(profile_url)
                if not profile_match:
                    activity_data = parse_strava_activity(
                        source_url, session_cookie=session_cookie)
                    athlete_id = (activity_data or {}).get('id_usuario_strava')
                    if not athlete_id or not str(athlete_id).isdigit():
                        raise RuntimeError('Nao foi possivel identificar o atleta a partir do link informado.')
                    profile_url = f'https://www.strava.com/athletes/{athlete_id}'
                    profile_match = STRAVA_PROFILE_RE.match(profile_url)

                # Piso de participacao: a importacao respeita o mes do PRIMEIRO
                # link do atleta no grupo (ou a fundacao do grupo da cidade, se
                # a caixa "ignorar participacao" estiver marcada) e pula meses
                # anteriores a isso — km de quando ele nao participava nao
                # entram no ranking.
                atleta_strava_id = profile_match.group(1)
                periodos_efetivos, aviso_participacao = _periodos_respeitando_participacao(
                    cidade, atleta_strava_id, periodos,
                    ignorar_participacao=bool(params.get('ignorar_participacao')),
                )
                if aviso_participacao:
                    _progress('reading', aviso_participacao, 12)

                _progress('scraping', 'Lendo o historico do atleta no Strava...', 15)
                throttle_perfil = build_strava_throttle(
                    mode=throttle_profile,
                    cidade=cidade,
                    cancel_check=lambda: _is_cancelled(job_id),
                    progress_callback=_progress,
                )

                # PROCESSAMENTO MES A MES: lista, raspa, salva -- e SO DEPOIS
                # passa para o proximo mes (20/08/2026, ERROS.md 4.15/4.16).
                #
                # Ate hoje a listagem inteira era feita primeiro (todos os
                # meses pedidos), e SO DEPOIS de terminar tudo e que a
                # validacao/gravacao rodava, de uma vez so, para o historico
                # inteiro. Se a listagem morresse no meio (mes 43 de 62), a
                # rodada acabava com ZERO atividades salvas, mesmo com 42
                # meses ja listados com sucesso -- foi exatamente o que
                # aconteceu ao vivo hoje. Agora cada mes e listado E validado
                # E salvo antes de passar para o proximo: um travamento no
                # meio deixa tudo que ja passou de verdade gravado no banco.
                # _periodos_respeitando_participacao sempre devolve um
                # conjunto nao-vazio (ou levanta RuntimeError antes de
                # chegar aqui), entao meses nunca fica vazio.
                perfil_restrito = None
                athlete_id = params.get('athlete_id') or atleta_strava_id
                if athlete_id and params.get('athlete_id') != athlete_id:
                    # Link de ATIVIDADE, nao de perfil: so agora se sabe
                    # de quem e. Gravar aqui faz a tela e o log passarem
                    # a mostrar o nome tambem nesses casos.
                    params['athlete_id'] = athlete_id
                    try:
                        _set_job_params(job_id, params)
                    except Exception as _exc_aid:
                        logger.debug('Nao consegui gravar athlete_id '
                                     'em %s: %s', job_id, _exc_aid)
                meses_processados = {tuple(p) for p in (params.get('meses_processados') or []) if len(p) == 2}
                concluidos = list(params.get('links_concluidos') or [])
                _vistos = set(concluidos)
                acumulado = dict(params.get('acumulado') or {})
                for _chave_default in ('salvas', 'outro_atleta', 'outro_periodo', 'modalidade', 'falhas', 'ja_no_banco'):
                    acumulado.setdefault(_chave_default, 0)
                total_urls_vistas = int(params.get('total_urls_vistas') or 0)
                # QUEM PODE RECEBER KM, e QUEM FOI BARRADO (28/08/2026).
                # `terceiros` sobrevive a retomada de proposito: e' o
                # relatorio que o dono le no fim para descobrir, por exemplo,
                # um atleta da cidade que refez o perfil no Strava e por isso
                # aparece com outro id.
                donos_aceitos = _donos_aceitos(cidade, athlete_id)
                terceiros = dict(params.get('terceiros') or {})

                # PRE-FILTRO POR ANO + ORDEM DE VARREDURA (21-22/08/2026).
                #
                # "todos os periodos" (periodos, o parametro ORIGINAL do
                # formulario, veio None) e "ano inteiro" (throttle_profile ==
                # 'ano_especifico') sao varreduras EM MASSA -- nenhum mes
                # especifico foi escolhido a dedo, o intervalo pedido e uma
                # bacia, nao uma lista que precisa ser lida inteira mes a mes.
                #
                # Uma tentativa anterior tentava adivinhar isso andando do mes
                # mais recente para o mais antigo e parando apos N meses
                # vazios seguidos -- REJEITADA com razao (dono, 21/08): atleta
                # pode sumir 2+ anos do Strava e voltar depois, e aquilo
                # pularia esse retorno. A substituicao e uma PROVA, nao uma
                # suposicao por silencio: o total de km de um ANO inteiro e a
                # soma dos totais dos 12 meses dele, entao um ano com total
                # confirmado <= 0 prova que nenhum mes dele tem atividade.
                # Falha de leitura ou ano com total > 0: NUNCA pula, cai na
                # varredura mes a mes de sempre -- que ja e rapida por
                # construcao, porque `extract_strava_profile_activity_urls`
                # faz list+total num unico pedido por mes e so aciona raspagem
                # de atividade quando o total do MES vier > 0 (nada muda aqui).
                #
                # A ordem final continua do mes mais recente para o mais
                # antigo (pedido do dono: comecar pelo mais proximo e bom) --
                # o que mudou nao foi a ordem, foi parar de presumir vazio por
                # silencio. Com a cobertura garantida pela prova matematica,
                # inverter a ordem final e so UX, sem risco de perder km.
                #
                # "Periodo especifico" (mes/lista escolhida a mao) nunca entra
                # nisso: sempre a lista exata pedida, sem pre-filtro, sem
                # reordenar -- e escolha deliberada do admin, nenhum mes pode
                # ser pulado por estar vazio.
                varredura_completa = periodos is None or throttle_profile == 'ano_especifico'

                # O PRE-FILTRO ANUAL SAIU (28/08/2026). Ele pedia o total do
                # ANO (`interval_type=year`) para provar que um ano inteiro
                # estava vazio e pular os 12 meses de uma vez. Medido hoje,
                # em 6 variantes de parametro: o Strava responde **HTTP 200
                # com corpo VAZIO**, ou 400 -- nunca com o bloco `#totals`.
                # `_totais_do_intervalo` devolve None nos dois casos e None
                # nunca pula, entao ele nao chegou a causar perda de mes; so
                # gastava uma requisicao por ano para nao concluir nada (4
                # anos seguidos com 400 no log desta madrugada).
                #
                # E o que ele economizava deixou de importar: com a listagem,
                # um mes custa UMA requisicao, entao varrer 64 meses custa 64.
                meses_pulados_por_ano = set()
                meses = sorted(
                    (m for m in periodos_efetivos if m not in meses_pulados_por_ano),
                    reverse=varredura_completa,
                )
                total_meses = len(meses)

                def _marcar_concluido(act_id, desfecho):
                    if act_id in _vistos:
                        return
                    _vistos.add(act_id)
                    concluidos.append(act_id)
                    # GRAVA NA HORA, sem juntar em lote. A primeira versao
                    # gravava a cada 10 links ou 20 segundos e perdeu
                    # exatamente a marca que importava: em 20/08 21h29 o job
                    # congelou no link seguinte a uma gravacao, e como o
                    # acumulo so e descarregado pela marca SEGUINTE, o link
                    # travado voltou para a rodada de retomada. Uma escrita
                    # pequena a cada ~14 segundos (o ritmo que o limite do
                    # Strava impoe, regra 4-C) e barata; repetir requisicao
                    # ao Strava nao e.
                    params['links_concluidos'] = list(concluidos)
                    _set_job_params(job_id, params)

                txt_folder = current_app.config['UPLOAD_FOLDER_TXT']
                os.makedirs(txt_folder, exist_ok=True)

                cancelado = False
                pausado = False
                algum_pendente = False
                for indice, (ano, mes) in enumerate(meses, 1):
                    if (ano, mes) in meses_processados:
                        continue
                    if _is_cancelled(job_id):
                        cancelado = True
                        break
                    # CONGELA ANTES DE O SERVIDOR DESLIGAR (26/08/2026).
                    # Este e' o unico ponto seguro: o mes anterior ja esta
                    # gravado no checkpoint, entao parar aqui nao custa nem uma
                    # requisicao repetida ao Strava. Parar no meio de um mes
                    # custaria refazer aquele mes inteiro amanha.
                    if _energia.perto_de_parar():
                        pausado = True
                        break

                    base_pct = 15 + int(75 * (indice - 1) / max(total_meses, 1))
                    fatia = 75 / max(total_meses, 1)
                    _progress('scraping',
                             'Mes %02d/%d (%d de %d): lendo o perfil no Strava...'
                             % (mes, ano, indice, total_meses), base_pct)

                    # UM MES, UMA REQUISICAO (28/08/2026). A listagem do
                    # /interval ja traz id, dono, esporte, data, distancia,
                    # desnivel e tempo de cada atividade -- conferido contra o
                    # nosso banco em 126 atividades, 126 bateram, nenhuma
                    # divergiu. Antes disto o mes custava uma requisicao para
                    # listar MAIS UMA POR ATIVIDADE: so o 08/2022 de um atleta
                    # gastava 77. Como o teto do Strava e' 100 a cada 15
                    # minutos, era isso que fazia uma importacao atravessar a
                    # noite.
                    def _progress_mes(stage, message, pct, _base=base_pct, _fatia=fatia,
                                      _mes=mes, _ano=ano, _i=indice):
                        pct = max(0, min(100, pct))
                        _progress(stage, 'Mes %02d/%d (%d de %d): %s'
                                 % (_mes, _ano, _i, total_meses, message),
                                 _base + int(_fatia * pct / 100))

                    results = None
                    try:
                        results = _gravar_mes_pela_listagem(
                            athlete_id=athlete_id, ano=ano, mes=mes,
                            cidade=cidade, admin_id=admin_id, origem=origem,
                            session_cookie=session_cookie,
                            throttle=throttle_perfil,
                            donos_aceitos=donos_aceitos,
                            cancel_check=lambda: _is_cancelled(job_id),
                            ids_ja_tentados=_vistos,
                            on_link_concluido=_marcar_concluido,
                            progress_callback=_progress_mes,
                            terceiros=terceiros,
                            job_id=job_id,
                        )
                    except StravaLimiteDeVolume as limite:
                        # CONGELA, NAO MORRE (regra do dono, 27/08/2026). O
                        # teto e' NOSSO: nada falhou. O mes anterior ja esta
                        # no checkpoint, entao parar aqui nao custa nem uma
                        # requisicao repetida -- a volta e' pelo relogio.
                        params['pausa_programada'] = True
                        params['pausa_ate'] = _time.time() + limite.segundos
                        params['pausado_em'] = _time.time()
                        params['terceiros'] = terceiros
                        params['acumulado'] = acumulado
                        _set_job_params(job_id, params)
                        volta = datetime.now() + timedelta(seconds=limite.segundos)
                        _progress('reading',
                                 'Pausado no mes %02d/%d — teto de requisições ao '
                                 'Strava (%s). Volta sozinho às %s.'
                                 % (mes, ano, limite.janela, volta.strftime('%H:%M')),
                                 base_pct)
                        logger.warning(
                            'Importacao %s pausada pelo teto (%s), volta as %s',
                            job_id, limite.janela, volta.strftime('%H:%M'))
                        return
                    except (ScrapeCancelled, StravaRateLimited, StravaAuthRequired):
                        raise
                    except StravaPerfilRestrito as exc_restrito:
                        # O ATLETA NAO NOS DEIXA VER (07/09/2026, P-130).
                        #
                        # Nao adianta o proximo mes: a restricao e do
                        # PERFIL, nao do periodo. Antes isso virava
                        # "nao consegui ler" e o job descia dez meses
                        # seguidos gastando uma requisicao em cada, para
                        # terminar dizendo "nenhuma atividade encontrada"
                        # -- sem ninguem saber o motivo real.
                        #
                        # Isto TEM conserto, mas do lado de la: a pessoa
                        # abre o perfil ou aceita a nossa conta como
                        # seguidora. Por isso para e AVISA com nome, em
                        # vez de insistir calado.
                        logger.warning('Importacao %s: %s', job_id,
                                       exc_restrito)
                        perfil_restrito = str(exc_restrito)
                        break
                    except StravaMesAnteriorAConta as exc_antes:
                        # CHEGOU AO COMECO DA CONTA: para de descer.
                        #
                        # A varredura vai do mes mais recente para tras. Ao
                        # bater no primeiro mes anterior a criacao da conta,
                        # TODOS os anteriores tambem sao -- continuar so
                        # gasta cota do Strava perguntando por meses que
                        # nunca existiram. Medido em 28/08/2026: 57
                        # requisicoes num perfil so, 156 num dia.
                        #
                        # So vale na varredura para tras. Quando o admin
                        # pediu meses especificos, cada um e um pedido
                        # explicito e nao ha 'para tras' que faca sentido.
                        logger.info('Importacao %s: %s Paro de descer aqui.',
                                    job_id, exc_antes)
                        _progress('scraping',
                                  'Cheguei ao comeco do histórico deste '
                                  'atleta (%02d/%d). Não adianta procurar '
                                  'mais para trás.' % (mes, ano), base_pct)
                        meses_processados.add((ano, mes))
                        if varredura_completa:
                            break
                        continue
                    except StravaListaVazia as exc_vazia:
                        # LEITURA FALHA NAO E' MES VAZIO (regra 4-G). NAO
                        # marca o mes como concluido: a proxima retomada
                        # relista, que custa uma requisicao so.
                        algum_pendente = True
                        logger.warning('Mes %02d/%d do perfil %s: %s',
                                       mes, ano, athlete_id, exc_vazia)
                        _progress('scraping',
                                 'Mes %02d/%d (%d de %d): não consegui ler a lista '
                                 '— fica para a próxima passada.'
                                 % (mes, ano, indice, total_meses), base_pct)
                        continue
                    except Exception as exc_mes:
                        # ERRO NAO CATEGORIZADO (20/08/2026). Um mes com
                        # problema nao pode derrubar os outros 61: loga com
                        # detalhe, NAO marca como processado e segue.
                        logger.error(
                            'ERRO NAO CATEGORIZADO ao listar %02d/%d do perfil %s: %s: %s',
                            mes, ano, athlete_id, type(exc_mes).__name__, exc_mes,
                            exc_info=True)
                        _progress('scraping',
                                 'Mes %02d/%d (%d de %d): erro inesperado ao listar '
                                 '(%s). Pulando por ora -- tentar de novo mais tarde.'
                                 % (mes, ano, indice, total_meses, type(exc_mes).__name__),
                                 base_pct)
                        continue
                    finally:
                        # O que foi resolvido ate aqui vale mesmo se a rodada
                        # terminou mal: e' justamente a retomada que precisa disso.
                        params['links_concluidos'] = list(concluidos)
                        params['terceiros'] = terceiros
                        _set_job_params(job_id, params)

                    total_urls_vistas += int(results.get('total_listadas') or 0)
                    params['total_urls_vistas'] = total_urls_vistas

                    if results.get('cancelled'):
                        cancelado = True
                        break

                    for chave, campo in (
                        ('salvas', 'activities_processed'),
                        ('outro_atleta', 'ignoradas_outro_atleta'),
                        ('outro_periodo', 'ignoradas_outro_periodo'),
                        ('modalidade', 'ignoradas_modalidade'),
                        ('falhas', 'activities_failed'),
                    ):
                        acumulado[chave] = int(acumulado.get(chave) or 0) + int(results.get(campo) or 0)
                    acumulado['ja_no_banco'] = max(
                        int(acumulado.get('ja_no_banco') or 0),
                        int(results.get('activities_skipped') or 0))

                    # O MOTIVO DE CADA UMA, NAO SO O TOTAL (27/08/2026).
                    # `pending_details` chegava aqui pronto e era descartado:
                    # o dono via "52 nao puderam ser lidas" sem saber quais
                    # nem por que. Agora a lista fica no trabalho, com teto --
                    # o contador continua inteiro, so a lista e' cortada.
                    pendencias = list(params.get('pendencias') or [])
                    for _p in (results.get('pending_details') or []):
                        if len(pendencias) >= _TETO_PENDENCIAS:
                            break
                        pendencias.append({
                            'url':      _p.get('url'),
                            'id':       _p.get('activity_id') or '',
                            'motivo':   _p.get('reason') or '',
                            'etapa':    _p.get('stage') or '',
                            'repetir':  bool(_p.get('retryable')),
                            # Quem postou e quando (15/09/2026): o admin precisa
                            # disso para pedir o link certo a pessoa.
                            'autor':    _p.get('autor') or '',
                            'quando':   _p.get('quando') or '',
                            'mes':      '%02d/%d' % (mes, ano),
                        })
                    params['pendencias'] = pendencias
                    params['acumulado'] = acumulado

                    # SO marca o mes como concluido se ele terminou de
                    # verdade. Mes que bateu teto de tempo, foi bloqueado, OU
                    # que a propria reconciliacao interna do process_whatsapp_txt
                    # (RECONCILIACAO no log) apontou candidato sem classificacao
                    # fica pendente -- a proxima retomada relista (barato) e so
                    # raspa o que ainda nao foi resolvido (ids_ja_tentados evita
                    # repetir requisicao ao Strava, regra 4-C). Antes disto
                    # (20-21/08/2026) a reconciliacao so gravava um aviso no log
                    # e o mes era marcado concluido do mesmo jeito -- o gap
                    # ficava registrado e esquecido, nunca reaberto.
                    if (results.get('deadline_hit') or results.get('rate_limited')
                            or results.get('reconciliacao_pendente')):
                        algum_pendente = True
                        if results.get('reconciliacao_pendente'):
                            logger.warning(
                                'Mes %02d/%d: %s atividade(s) sem classificacao '
                                '(reconciliacao) -- mes NAO marcado como concluido, '
                                'proxima retomada relista.',
                                mes, ano, results.get('reconciliacao_pendente'))
                    else:
                        meses_processados.add((ano, mes))
                    params['meses_processados'] = [[a, m] for a, m in sorted(meses_processados)]
                    _set_job_params(job_id, params)

                    _progress('reading',
                             'Mes %02d/%d concluido: %d salva(s), %d de outra pessoa, '
                             '%d de outro mes, %d de modalidade, %d nao lida(s).'
                             % (mes, ano, int(results.get('activities_processed') or 0),
                                int(results.get('ignoradas_outro_atleta') or 0),
                                int(results.get('ignoradas_outro_periodo') or 0),
                                int(results.get('ignoradas_modalidade') or 0),
                                int(results.get('activities_failed') or 0)),
                             base_pct + int(fatia))

                if cancelado:
                    _set_progress(job_id, 'cancelled', 'Importacao cancelada pelo usuario.', 0, origem, cidade)
                    _expire_job(job_id, 24 * 60 * 60)
                    return

                if pausado:
                    # PAUSA PROGRAMADA NAO E' FALHA. O estagio nao e' terminal
                    # de proposito: o trabalho continua existindo, com o
                    # checkpoint intacto, e volta sozinho quando a maquina
                    # voltar (`_maybe_resume_job` compara com a hora do boot).
                    # `pausado_em` e `pausa_programada` sao o que impede esta
                    # parada de gastar uma das 3 tentativas de retomada --
                    # gastar seria punir o trabalho por um desligamento que ele
                    # nao causou.
                    feitos = len(meses_processados)
                    params['pausado_em'] = _time.time()
                    params['pausa_programada'] = True
                    _set_job_params(job_id, params)
                    voltar = _energia.horario_de_voltar()
                    quando = ('Continua sozinha quando o servidor voltar, por volta '
                              'das %02d:%02dh.' % voltar) if voltar else                              'Continua sozinha assim que o servidor voltar.'
                    _set_progress(
                        job_id, 'pausado',
                        'Pausada porque o servidor vai desligar. %d mes(es) ja '
                        'lido(s) e salvo(s). %s Nada se perde e nada precisa ser '
                        'refeito.' % (feitos, quando),
                        max(15, int(15 + 75 * feitos / max(total_meses, 1))),
                        origem, cidade,
                    )
                    logger.warning('Job %s pausado antes do desligamento (%d mes(es) prontos)',
                                   job_id, feitos)
                    return

                if not athlete_id:
                    raise RuntimeError('Nao foi possivel identificar o atleta a partir do link informado.')
                if total_urls_vistas == 0:
                    if not session_cookie:
                        raise RuntimeError(
                            'Nenhuma atividade encontrada. O Strava nao lista mais o historico '
                            'de um perfil sem sessao: peca ao master para configurar o cookie '
                            'do Strava em Painel > Sessao do Strava e tente de novo.'
                        )
                    # QUANDO SE SABE O MOTIVO, DIZ-SE O MOTIVO (07/09/2026).
                    # 'confira se o perfil e visivel' mandava o admin
                    # investigar o que o codigo ja tinha descoberto.
                    if perfil_restrito:
                        raise RuntimeError(
                            '%s Peca a ele para tornar as atividades publicas, ou '
                            'para aceitar a conta do projeto como seguidora — o '
                            'quilometro existe, so nao esta visivel para nos.'
                            % perfil_restrito)
                    raise RuntimeError(
                        'Nenhuma atividade encontrada no perfil para o periodo selecionado. '
                        'Confira o periodo e se o perfil e visivel para a conta do cookie.'
                    )

                # Aproveita a sessao ativa para guardar/renovar a foto do atleta.
                try:
                    from app.blueprints.conferencia import _atualizar_foto_atleta
                    _atualizar_foto_atleta(athlete_id, session_cookie, throttle_perfil)
                except Exception as exc_foto:
                    logger.debug('Foto do atleta nao atualizada na importacao: %s', exc_foto)

                # O feed mensal do perfil inclui atividades de TREINOS EM GRUPO
                # (de outras pessoas). Importacao e do atleta pedido: qualquer
                # atividade de outro dono salva por ESTA importacao e removida.
                de_outros = (
                    Activity.query
                    .filter_by(cidade=cidade, arquivo_origem=origem)
                    .filter(Activity.id_usuario_strava != str(atleta_strava_id))
                    .delete(synchronize_session=False)
                )
                if de_outros:
                    db.session.commit()
                    logger.info('Importacao %s: %s atividade(s) de outros atletas '
                                '(treino em grupo) descartadas', origem, de_outros)

                periodos_salvos = {
                    (atividade.data_hora.year, atividade.data_hora.month)
                    for atividade in Activity.query.filter_by(
                        cidade=cidade, arquivo_origem=origem
                    ).filter(Activity.data_hora.isnot(None)).all()
                }
                if periodos_salvos:
                    _invalidar_consolidacao(cidade, periodos_salvos)

                # O TOTAL E DO JOB INTEIRO, NAO DA ULTIMA PERNA.
                # Uma importacao retomada roda em pedacos, e cada pedaco so
                # enxerga o que ele mesmo fez: em 20/08 a tela fechou dizendo
                # "0 salvas, 2 de outras pessoas" numa rodada que tinha
                # resolvido 23 links — os outros 19 tinham sido de uma perna
                # anterior. Numero que so conta o ultimo pedaco faz o dono
                # achar que o trabalho se perdeu.
                # Somam-se os desfechos; "ja estavam no ranking" NAO se soma
                # (e uma contagem contra o banco, repetida igual em cada
                # perna) — dela vale a maior. Isso agora e feito MES A MES,
                # dentro do loop acima, entao `acumulado` ja chega pronto aqui.
                salvas = acumulado['salvas']
                ja_tinha = acumulado['ja_no_banco']
                mensagem = f'Importacao concluida. {salvas} atividade(s) salva(s).'
                if aviso_participacao:
                    mensagem += f' {aviso_participacao}'
                if ja_tinha:
                    mensagem += f' {ja_tinha} ja estavam no ranking.'
                # CADA MOTIVO COM O NOME DELE (20/08/2026). A frase unica
                # anterior dizia "fora do periodo ou de modalidade nao
                # pontuada" para tudo, e o dono leu "180 fora" numa importacao
                # do proprio perfil em que 100% das ignoradas eram treino em
                # grupo de OUTRAS pessoas (regra 4-D) — nem uma por periodo,
                # nem uma por modalidade. Motivo errado na tela faz duvidar de
                # km que estao certos. Ver ERROS.md e o plano
                # docs/planos/2026-08-20_importacao-em-looping.md.
                de_outros_atletas = acumulado['outro_atleta']
                de_outro_periodo = acumulado['outro_periodo']
                de_modalidade = acumulado['modalidade']
                nao_lidas = acumulado['falhas']
                # POR TIPO, nao um numero so: "pode nao ser apenas um tipo de
                # erro" (dono, 27/08). Sai a contagem de cada motivo e quantas
                # valem a pena repetir.
                _pend = params.get('pendencias') or []
                _por_motivo = {}
                for _p in _pend:
                    _por_motivo[_p.get('motivo') or 'sem motivo registrado'] = (
                        _por_motivo.get(_p.get('motivo') or 'sem motivo registrado', 0) + 1)
                _retentaveis = sum(1 for _p in _pend if _p.get('repetir'))
                if de_outros_atletas:
                    mensagem += (
                        f' {de_outros_atletas} eram de outras pessoas '
                        f'(treino em grupo no Strava) e nao entram no seu total.')
                if de_outro_periodo:
                    mensagem += f' {de_outro_periodo} sao de outro mes.'
                if de_modalidade:
                    mensagem += (
                        f' {de_modalidade} sao de modalidade que nao pontua '
                        f'(so contam corrida/caminhada e ciclismo).')
                # NAO SE OMITE O QUE NAO DEU CERTO, E NAO SE PROMETE O QUE
                # NAO ACONTECE (27/08/2026). Ate hoje a frase dizia "repetir a
                # importacao tenta de novo" para TUDO. Medido na noite de
                # 26/08: as 76 falhas eram todas leitura de pagina, e repetir
                # falharia igual -- o Strava devolvia a casca de login com
                # HTTP 200 porque a raspagem de atividade ia sem cookie.
                # Agora cada motivo aparece com o nome dele, e so quem e'
                # retentavel ganha a promessa de que repetir adianta.
                if nao_lidas:
                    mensagem += (
                        f' {nao_lidas} nao puderam ser lidas no Strava; '
                        f'repetir a importacao tenta de novo.')
                # Rodada interrompida por teto de tempo/bloqueio em algum mes:
                # a importacao nao tem continuacao automatica, mas repetir e
                # barato (o que ja foi salvo e deduplicado, e o mes pendente
                # nao foi marcado como concluido, entao a proxima retomada
                # continua exatamente dali).
                if algum_pendente:
                    mensagem += (' Nem tudo coube nesta rodada: repita a importacao '
                                 'mais tarde para continuar de onde parou.')
                _set_progress(job_id, 'done', mensagem, 100, origem, cidade)
                _expire_job(job_id, 24 * 60 * 60)
            except ScrapeCancelled:
                db.session.rollback()
                _set_progress(job_id, 'cancelled', 'Importacao cancelada pelo usuario.', 0, origem, cidade)
                _expire_job(job_id, 24 * 60 * 60)
            except StravaRateLimited:
                db.session.rollback()
                logger.warning('Importacao do atleta interrompida por bloqueio do Strava')
                _set_progress(
                    job_id, 'error',
                    'O Strava limitou o acesso durante a leitura do perfil. Aguarde alguns '
                    'minutos e repita a importacao (o que ja foi salvo continua no ranking).',
                    0, origem, cidade,
                )
                _expire_job(job_id, 24 * 60 * 60)
            except StravaAuthRequired as exc:
                db.session.rollback()
                logger.warning('Importacao do atleta sem sessao valida no Strava: %s', exc)
                secretstore.record_session_health(
                    cidade, False,
                    'A sessao do Strava expirou ou nao esta configurada.')
                _set_progress(
                    job_id, 'error',
                    'A sessao do Strava expirou ou nao esta configurada. Peca ao master '
                    'para atualizar o cookie em Painel > Sessao do Strava e repita a '
                    'importacao (nada foi perdido: o que ja foi salvo continua no ranking).',
                    0, origem, cidade,
                )
                _expire_job(job_id, 24 * 60 * 60)
            except Exception as exc:
                db.session.rollback()
                logger.error('Erro na importacao do atleta: %s', exc, exc_info=True)
                _set_progress(job_id, 'error', f'Erro na importacao: {exc}', 0, origem, cidade)
                _expire_job(job_id, 24 * 60 * 60)
            finally:
                _LOCAL_THREADS.pop(job_id, None)

    _background()


# ===== ROTAS =====

@upload_bp.route('/')
@login_required
@admin_required
def index():
    _ensure_upload_schema()
    form = UploadForm()
    processed_files = (
        ProcessedFile.query
        .filter_by(cidade=current_user.cidade_ativa)
        .order_by(ProcessedFile.processado_em.desc())
        .limit(10).all()
    )
    return render_template('upload/upload.html',
                           form=form,
                           processed_files=processed_files,
                           cidade=current_user.cidade_ativa)


@upload_bp.route('/pendencias/<int:pf_id>')
@login_required
@admin_required
def pending_details(pf_id):
    _ensure_upload_schema()
    pf = ProcessedFile.query.get(pf_id)
    if not pf:
        return render_template(
            'upload/pending_details.html',
            file=None,
            details=[],
            error_message='Registro nao encontrado.',
        ), 404
    if pf.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        return render_template(
            'upload/pending_details.html',
            file=pf,
            details=[],
            error_message='Sem permissao para visualizar este processamento.',
        ), 403

    try:
        details = json.loads(pf.pendencias_json or '[]')
        if not isinstance(details, list):
            details = []
    except Exception:
        details = []

    return render_template(
        'upload/pending_details.html',
        file=pf,
        details=details,
        error_message='',
    )


@upload_bp.route('/links', methods=['GET', 'POST'])
@login_required
@admin_required
def manual_links():
    results = []
    error_message = ''
    raw_links = ['']
    only_current_month = True
    periodos = None

    if _req.method == 'POST':
        from flask_wtf.csrf import validate_csrf
        from wtforms import ValidationError

        raw_links = _req.form.getlist('links') or ['']
        only_current_month = _req.form.get('process_current_month') in ('y', 'true', '1', 'on')
        try:
            periodos = _periodos_upload(_req.form, only_current_month)
        except ValueError as exc:
            error_message = str(exc)
            periodos = None
        try:
            validate_csrf(_req.form.get('csrf_token', ''))
        except ValidationError as exc:
            error_message = f'Token CSRF invalido: {exc}'
        if not error_message:
            urls = _extract_manual_strava_urls(raw_links)
            if not urls:
                error_message = 'Cole pelo menos um link valido de atividade do Strava.'
            else:
                saved_any = False
                periodos_salvos = set()
                for url in urls:
                    try:
                        item = _process_single_strava_url(
                            url,
                            cidade=current_user.cidade_ativa,
                            admin_id=current_user.id,
                            only_current_month=only_current_month,
                            periodos=periodos,
                        )
                    except Exception as exc:
                        db.session.rollback()
                        logger.error('Erro ao processar link avulso %s: %s', url, exc, exc_info=True)
                        item = {
                            'url': url,
                            'status': 'erro',
                            'message': f'Erro inesperado: {exc}',
                        }
                    if item.get('status') == 'salvo':
                        saved_any = True
                        if item.get('periodo'):
                            periodos_salvos.add(tuple(item['periodo']))
                    results.append(item)

                if saved_any:
                    _invalidar_consolidacao(current_user.cidade_ativa, periodos_salvos or periodos)

    return render_template(
        'upload/manual_links.html',
        results=results,
        error_message=error_message,
        raw_links=raw_links,
        only_current_month=only_current_month,
        periodos=periodos,
        cidade=current_user.cidade_ativa,
    )


@upload_bp.route('/atleta', methods=['GET', 'POST'])
@login_required
@admin_required
def importar_atleta():
    """Importa atividades publicas de um atleta, classificadas pela data Strava."""
    error_message = ''
    job_id = None
    source_url = ''
    periodos = None

    modo_periodo = 'periodo_especifico'

    if _req.method == 'POST':
        from flask_wtf.csrf import validate_csrf
        from wtforms import ValidationError

        source_url = (_req.form.get('link_atleta') or '').strip()
        modo_periodo = (_req.form.get('periodo_upload') or '').strip() or 'periodo_especifico'
        ignorar_participacao = (_req.form.get('ignorar_participacao') or '') == 'on'
        try:
            validate_csrf(_req.form.get('csrf_token', ''))
            periodos = _periodos_upload(_req.form, process_current_month=False)
            if not periodos and modo_periodo != 'todos_periodos':
                raise ValueError('Selecione um mes/ano, um ano ou a opcao de todos os periodos.')
            # Link do proprio site (07/09/2026): /ath/<id> ou
            # /admin/atletas/<id>/ajuste-manual viram o link de perfil Strava
            # equivalente, e o resto do fluxo segue igual.
            convertido = resolver_link_do_sistema(source_url)
            if convertido:
                source_url = convertido
            if STRAVA_SHORT_RE.match(source_url):
                source_url = resolve_strava_shortlink(source_url) or source_url
            if not (STRAVA_PROFILE_RE.match(source_url) or STRAVA_DIRECT_RE.match(source_url)):
                raise ValueError('Informe um link de perfil ou de atividade do Strava.')
            # 20/08/2026 — CONFERE AQUI, SINCRONO, ANTES DE INICIAR O JOB.
            # Achado ao vivo: a mesma checagem dentro de
            # `_periodos_respeitando_participacao` roda DENTRO da thread de
            # fundo, e quando a cidade nao tem piso cadastrado ela falha em
            # milissegundos -- rapido demais para o "job sobrevive ao
            # redirect" (`_job_atleta_em_andamento` ja ignora job terminal)
            # conseguir mostrar qualquer coisa. A tela parecia travada: clicou
            # e nada aconteceu. Falha rapida tem de aparecer na MESMA pagina,
            # como qualquer outro erro de formulario.
            if ignorar_participacao and not _piso_importacao_cidade(current_user.cidade_ativa):
                raise ValueError(
                    'Esta cidade ainda nao tem a fundacao do grupo cadastrada '
                    '(Painel > Cidades > Editar). Cadastre o mes/ano antes de usar '
                    'esta opcao, ou desmarque-a para usar o piso normal (primeiro '
                    'link deste atleta no grupo).'
                )
            # 20/08/2026 — NAO DEIXA COMECAR UMA SEGUNDA IMPORTACAO NA MESMA
            # CIDADE. Achado ao vivo: o clique em "Iniciar importacao" nao
            # conferia se ja havia uma em andamento — um clique duplo (ou um
            # segundo clique por impaciencia, sem o redirect ainda aplicado)
            # criava um job NOVO competindo pelo mesmo Strava com o job que
            # ja estava rodando. As duas ficavam se revezando na fila
            # compartilhada (`ratepacer.py`), cada uma vendo "Outro
            # processamento esta usando o Strava" — e nenhuma das duas
            # terminava nunca. `_job_atleta_em_andamento` ja existia para
            # MOSTRAR o job ativo; faltava usa-lo para RECUSAR um segundo.
            # 27/08/2026 — SEM VAGA E' FILA, TAMBEM NO CLIQUE.
            # A protecao de 20/08 (acima) continua valendo, mas ela conferia a
            # coisa errada: qualquer trabalho em andamento fazia o pedido NOVO
            # ser jogado fora sem virar nada. O dono mandou 5 atletas de uma vez
            # e viu um so na tela. O que caracteriza clique duplo e' pedir O
            # MESMO PERFIL de novo -- outro atleta e' outro trabalho, e o lugar
            # dele e' a fila, que ja existe (`_ESTAGIO_NA_FILA`).
            # Reaproveita a varredura de `_trabalhos_em_aberto`: nao existe uma
            # terceira leitura da mesma pasta.
            job_id = None
            for _t in _trabalhos_em_aberto(current_user.cidade_ativa):
                if (_t.get('kind') == 'importacao_atleta'
                        and _t.get('source_url') == source_url):
                    job_id = _t['id']          # e' o mesmo pedido, nao um novo
                    break
            if not job_id:
                job_id = _iniciar_importacao_atleta(
                    source_url=source_url,
                    cidade=current_user.cidade_ativa,
                    admin_id=current_user.id,
                    periodos=periodos,
                    throttle_profile=modo_periodo,
                    ignorar_participacao=ignorar_participacao,
                )
            # 20/08/2026 — REDIRECIONA DEPOIS DO POST (padrao Post/Redirect/Get).
            # ACHADO AO VIVO, com o dono na tela: sem isto, a pagina que ele via
            # depois de clicar "Iniciar importacao" era a RESPOSTA DIRETA do
            # POST. Um F5 nessa pagina faz o navegador reenviar o formulario
            # ("Confirmar reenvio de formulario" — o aviso de "pode perder
            # dado" que ele via nao era deste app, era o proprio navegador) —
            # e reenviar o POST chama `_iniciar_importacao_atleta` DE NOVO,
            # criando um job_id novo do zero. Era por isso que a importacao
            # "voltava para o mes 1 de 62" toda vez que ele atualizava a
            # pagina para conferir se o cancelamento tinha funcionado: nao
            # era o job antigo voltando, era um job NOVO nascendo a cada F5.
            # Redirecionar para o GET faz a pagina que fica aberta ser segura
            # de recarregar — GET nunca reenvia nada.
            return redirect(url_for('upload.importar_atleta'))
        except ValidationError as exc:
            # 20/08/2026 — ANTES ISTO FALHAVA CALADO (so `error_message`, sem
            # log nenhum), entao quando o dono relatava "deu erro de CSRF" nao
            # havia como confirmar na fonte. `WTF_CSRF_TIME_LIMIT = None`
            # (config.py) ja tira a expiracao por relogio; o que sobra para
            # invalidar o token e a SESSAO ter mudado (login de novo em outra
            # aba, cookie perdido) — nao tem relacao com a duracao da
            # importacao em si. Mensagem em portugues, com a saida clara.
            logger.warning('CSRF invalido em /upload/atleta (cidade=%s, usuario=%s): %s',
                           current_user.cidade_ativa, current_user.email, exc)
            error_message = ('A página expirou (sessão renovada em outra aba, ou você '
                             'demorou para enviar). Isto não afeta nenhuma importação já '
                             'em andamento — role a página e recarregue (F5) antes de '
                             'tentar de novo.')
        except ValueError as exc:
            error_message = str(exc)

    # A IMPORTACAO SOBREVIVE A SAIR DA TELA (14/08/2026, pedido do dono).
    # Recarregar, fechar o navegador ou voltar depois passa por aqui: se ainda
    # ha uma importacao viva nesta cidade, a tela reencontra o acompanhamento e
    # o botao de cancelar. Antes, o trabalho continuava rodando invisivel — e
    # sem jeito de parar.
    if not job_id:
        job_id, _job = _job_atleta_em_andamento(current_user.cidade_ativa)

    return render_template(
        'upload/importar_atleta.html',
        cota=_resumo_da_cota(),
        error_message=error_message,
        job_id=job_id,
        source_url=source_url,
        periodos=periodos,
        modo_periodo=modo_periodo,
        cookie_strava=secretstore.info(secretstore.STRAVA_SESSION, scope=current_user.cidade_ativa),
        cidade=current_user.cidade_ativa,
        now=datetime.now(),
        # Quem vai comecar uma importacao de horas precisa saber que o servidor
        # desliga a noite -- e que isso nao estraga nada (26/08/2026).
        energia=_energia.resumo(),
        minutos_ate_parar=_energia.minutos_ate_parar(),
        # O supremo enxerga as outras cidades porque e' ele quem socorre quando
        # um trabalho de outra cidade esta pesando na maquina (regra 2).
        trabalhos=_trabalhos_em_aberto(
            current_user.cidade_ativa,
            todas_as_cidades=getattr(current_user, 'is_supremo', False)),
    )


def _job_e_da_minha_cidade(job):
    """O trabalho pertence a cidade em que este usuario esta operando?

    REGRAS 1 e 2 DO CLAUDE.MD. Ate 26/08/2026 as telas de andamento e de
    cancelamento agiam so pelo codigo do trabalho, sem conferir de quem ele era
    -- com uma cidade nao fazia diferenca, com tres faz. O supremo atravessa
    cidade por definicao, e quem decide isso e `cidade_ativa`, nunca comparar
    `tipo` na mao.
    """
    dono = (job or {}).get('cidade') or ''
    if not dono:
        return True          # trabalho antigo, sem cidade gravada
    if getattr(current_user, 'is_supremo', False):
        return True
    return dono == (current_user.cidade_ativa or '')


def _pode_cancelar(job):
    """(pode, motivo) — hierarquia manda no cancelamento (pedido do dono, 26/08).

    Admin nao cancela trabalho iniciado por master: quem comecou fica gravado
    no proprio trabalho (`params.admin_id`), entao da para conferir. Nivel
    IGUAL basta (dois admins da mesma cidade se destravam), o que falta e
    cancelar de cima para baixo. Sem dono gravado, libera: trabalho velho nao
    pode ficar impossivel de parar.
    """
    from app.models import Usuario
    dono_id = ((job or {}).get('params') or {}).get('admin_id')
    if not dono_id or getattr(current_user, 'is_supremo', False):
        return True, ''
    try:
        dono = Usuario.query.get(int(dono_id))
    except Exception:
        dono = None
    if dono is None or dono.id == current_user.id:
        return True, ''
    if current_user.nivel >= dono.nivel:
        return True, ''
    return False, ('Esta importação foi iniciada por %s, que tem acesso acima do seu. '
                   'Só ele pode cancelar.' % (dono.rotulo_tipo or 'outro usuário'))


@upload_bp.route('/cota', methods=['POST'])
@login_required
@admin_required
def regular_cota():
    """Muda o teto de requisicoes ao Strava. Vale na hora.

    POR QUE ISTO E UM BOTAO E NAO UMA VARIAVEL DE AMBIENTE (29/08/2026):
    o teto certo depende de quanto historico ainda falta importar, e isso
    muda toda semana. Deixar no `emperor.ini` do Web Station significaria
    recriar o vassal a cada ajuste -- e uma atualizacao do DSM apaga esse
    arquivo sozinha (COMANDOS.md secao 9).

    O numero e' do dono, e ele assume o risco: subir o teto aumenta a
    chance de o Strava reclamar. Quem protege de verdade e' a trava de
    `app/stravasinal.py`, que LE a resposta e para quando ele reclama.
    """
    from flask_wtf.csrf import validate_csrf
    from wtforms import ValidationError
    try:
        validate_csrf(_req.form.get('csrf_token', ''))
    except ValidationError as exc:
        logger.warning('Cota: token CSRF invalido (%s)', exc)
        return redirect(url_for('upload.importar_atleta'))
    try:
        from app.ratepacer import SharedPacer
        pedidos = {}
        for chave in ('q15', 'hora', 'dia'):
            bruto = (_req.form.get('teto_%s' % chave) or '').strip()
            if bruto:
                pedidos[chave] = int(float(bruto.replace(',', '.')))
        valendo = SharedPacer('strava').definir_tetos(
            pedidos, por=getattr(current_user, 'email', '') or '')
        # FICA NO LOG COM O NOME DE QUEM MUDOU: e' uma decisao de risco, e
        # daqui a um mes ninguem lembra quem escolheu o numero.
        logger.warning('Teto de requisicoes ao Strava alterado por %s: %s',
                       getattr(current_user, 'email', '?'), valendo)
    except (TypeError, ValueError):
        logger.warning('Cota: valor nao numerico enviado pela tela')
    except Exception as exc:
        logger.error('Falha ao regular a cota: %s', exc, exc_info=True)
    return redirect(url_for('upload.importar_atleta'))


@upload_bp.route('/status/<job_id>')
@login_required
@admin_required
def job_status(job_id):
    job = _job_progress.get(job_id) or _read_progress_file(job_id)
    if not job:
        return jsonify(stage='unknown', message='Acompanhamento do job nao encontrado.', pct=0)
    if not _job_e_da_minha_cidade(job):
        return jsonify(stage='unknown',
                       message='Acompanhamento do job nao encontrado.', pct=0), 404
    # A RETOMADA SAIU DAQUI (26/08/2026 — incidente do 502, ver ERROS.md).
    #
    # Esta rota e chamada em ciclo pelo navegador enquanto a tela esta aberta.
    # Chamar `_maybe_resume_job` aqui fazia cada consulta abrir e gravar
    # arquivos e ate disparar uma thread -- caro, e na hora errada: quando o
    # servidor esta sofrendo e' justamente quando o navegador mais pergunta.
    # Quem reanima trabalho interrompido e o ticker de fundo
    # (`start_resume_ticker`, de 2 em 2 minutos), que existe exatamente para
    # NAO depender do navegador aberto. Um dos dois basta, e nao e este.
    # O ESTAGIO REAL VAI SEMPRE JUNTO (14/08/2026).
    #
    # Antes, "sem resposta ha N minutos" SUBSTITUIA a mensagem do que estava
    # sendo feito, e o admin perdia a unica informacao util da tela — ficava
    # sabendo que algo demorou, sem saber em que ponto. Agora o aviso e um
    # campo separado: quem exibe decide mostrar os dois, um embaixo do outro.
    #
    # `stage='stale'` continua saindo porque a tela de upload de conversa
    # depende dele; quebrar aquele contrato para arrumar esta tela seria trocar
    # um defeito por outro.
    parado = _is_stale_job(job)
    minutos = int((_job_age_seconds(job) or 0) // 60)
    estagio_real = job.get('stage', 'unknown')
    mensagem_real = job.get('message', 'Processamento em andamento.')
    return jsonify(
        stage='stale' if parado else estagio_real,
        message=(
            f'Processamento sem resposta ha {minutos} minuto(s). '
            'Voce pode cancelar mantendo ou removendo as atividades parciais.'
        ) if parado else mensagem_real,
        pct=job.get('pct', 0),
        parado=parado,
        parado_minutos=minutos if parado else 0,
        estagio_real=estagio_real,
        mensagem_real=mensagem_real,
    )


@upload_bp.route('/cancel/<job_id>', methods=['POST'])
@login_required
@admin_required
def cancel_job(job_id):
    """Solicita cancelamento do job. delete=true apaga as atividades ja inseridas."""
    data = _req.get_json(silent=True) or {}
    delete = bool(data.get('delete', False))
    job = _job_progress.get(job_id) or _read_progress_file(job_id)

    if not job:
        return jsonify(status='error', message='Job nao encontrado.'), 404

    if not _job_e_da_minha_cidade(job):
        return jsonify(status='error', message='Job nao encontrado.'), 404

    pode, motivo = _pode_cancelar(job)
    if not pode:
        return jsonify(status='error', message=motivo), 403

    if _is_stale_job(job):
        try:
            message = _finalize_stale_cancel(job_id, delete=delete, job=job)
            return jsonify(status='ok', stage='cancelled', message=message)
        except Exception as exc:
            db.session.rollback()
            logger.error('Erro ao finalizar cancelamento do job travado %s: %s',
                         job_id, exc, exc_info=True)
            return jsonify(
                status='error',
                message=f'Erro ao finalizar cancelamento do job travado: {exc}',
            ), 500

    cancel_flag = {'delete': delete}
    _cancel_flags[job_id] = cancel_flag
    _write_cancel_file(job_id, cancel_flag)
    # CANCELAR PASSOU A SER PARADA DE FATO (26/08/2026): se o trabalho tem
    # processo proprio, ele para sozinho ao ver o pedido -- e se nao parar em
    # 30 s, e' encerrado. Antes, preso numa requisicao ao Strava, ele nao via
    # pedido nenhum e seguia trabalhando depois do clique.
    _garantir_parada(job_id, job)
    _set_progress(
        job_id,
        'cancelling',
        'Cancelamento solicitado. Aguardando a atividade atual terminar...',
        0,
        job.get('zip_filename', ''),
        job.get('cidade', ''),
    )
    logger.info(f"Cancel solicitado para job={job_id} delete={delete}")
    return jsonify(status='ok', message='Cancelamento solicitado.')


@upload_bp.route('/delete_history/<int:pf_id>', methods=['POST'])
@login_required
@admin_required
def delete_history(pf_id):
    """Remove um registro do historico de uploads."""
    _ensure_upload_schema()
    pf = ProcessedFile.query.get(pf_id)
    if not pf:
        return jsonify(status='error', message='Registro nao encontrado'), 404
    if pf.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        return jsonify(status='error', message='Sem permissao'), 403
    deleted_acts = _delete_upload_activities(pf.id, pf.arquivo_nome, pf.cidade)
    cidade_pf = pf.cidade
    db.session.delete(pf)
    db.session.commit()
    _invalidar_consolidacao(cidade_pf)
    logger.info(f'Historico {pf_id} deletado + {deleted_acts} atividades removidas')
    return jsonify(status='ok', atividades_removidas=deleted_acts)


@upload_bp.route('/chunk', methods=['POST'])
@login_required
@admin_required
def upload_chunk():
    """Recebe um chunk do arquivo ZIP para upload fragmentado."""
    csrf_err = _validate_csrf()
    if csrf_err:
        return csrf_err

    upload_id    = _req.form.get('upload_id', '').strip()
    chunk_index  = _req.form.get('chunk_index', '')
    total_chunks = _req.form.get('total_chunks', '')
    chunk_file   = _req.files.get('chunk')

    if not re.match(r'^[0-9a-f\-]{36}$', upload_id):
        return jsonify(status='error', message='upload_id invalido'), 400
    if not chunk_file or not chunk_index.isdigit() or not total_chunks.isdigit():
        return jsonify(status='error', message='Dados do chunk invalidos'), 400

    chunk_index  = int(chunk_index)
    total_chunks = int(total_chunks)

    chunk_dir  = os.path.join(current_app.config['TEMP_EXTRACT_FOLDER'], 'chunks', upload_id)
    os.makedirs(chunk_dir, exist_ok=True)

    chunk_path = os.path.join(chunk_dir, f'chunk_{chunk_index:06d}')
    chunk_file.save(chunk_path)

    logger.info(f'[CHUNK] upload_id={upload_id} idx={chunk_index} total={total_chunks}')
    return jsonify(status='ok', received=chunk_index, total=total_chunks)


@upload_bp.route('/assemble', methods=['POST'])
@login_required
@admin_required
def assemble_upload():
    """Reassembla chunks recebidos e inicia processamento."""
    csrf_err = _validate_csrf()
    if csrf_err:
        return csrf_err

    upload_id    = _req.form.get('upload_id', '').strip()
    filename     = _req.form.get('filename', 'upload.zip')
    total_chunks = _req.form.get('total_chunks', '0')
    process_cm   = _req.form.get('process_current_month') in ('y', 'true', '1', 'on')
    throttle_profile = (_req.form.get('periodo_upload') or '').strip() or None
    try:
        periodos = _periodos_upload(_req.form, process_cm)
    except ValueError as exc:
        return jsonify(status='error', message=str(exc)), 400

    if not re.match(r'^[0-9a-f\-]{36}$', upload_id):
        return jsonify(status='error', message='upload_id invalido'), 400
    if not total_chunks.isdigit() or int(total_chunks) < 1:
        return jsonify(status='error', message='total_chunks invalido'), 400

    total_chunks = int(total_chunks)
    chunk_dir    = os.path.join(current_app.config['TEMP_EXTRACT_FOLDER'], 'chunks', upload_id)

    missing = [i for i in range(total_chunks)
               if not os.path.exists(os.path.join(chunk_dir, f'chunk_{i:06d}'))]
    if missing:
        return jsonify(status='error',
                       message=f'Chunks ausentes: {missing[:5]}. Tente novamente.'), 400

    zip_folder  = current_app.config['UPLOAD_FOLDER_ZIP']
    os.makedirs(zip_folder, exist_ok=True)

    safe_name    = secure_filename(filename) or 'upload.zip'
    timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
    cidade_slug  = _storage_slug(current_user.cidade_ativa, 'cidade')
    zip_filename = f"{timestamp}_{cidade_slug}_{safe_name}"
    zip_path     = os.path.join(zip_folder, zip_filename)

    try:
        with open(zip_path, 'wb') as out_f:
            for i in range(total_chunks):
                cp = os.path.join(chunk_dir, f'chunk_{i:06d}')
                with open(cp, 'rb') as cf:
                    out_f.write(cf.read())
    except Exception as e:
        logger.error('Erro ao montar ZIP: %s', e)
        shutil.rmtree(chunk_dir, ignore_errors=True)
        return jsonify(status='error', message=f'Erro ao montar arquivo: {e}'), 500
    else:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        safe_log = zip_filename.encode('ascii', errors='replace').decode('ascii')
        logger.info('[ASSEMBLE] %s montado de %d chunks', safe_log, total_chunks)

    job_id = str(uuid.uuid4())
    _set_progress(job_id, 'upload', 'Arquivo reconstruido. Verificando integridade...', 20,
                  zip_filename, current_user.cidade_ativa)

    return _process_saved_zip(
        zip_path, zip_filename,
        current_user.cidade_ativa, current_user.id,
        process_cm, job_id, periodos=periodos,
        throttle_profile=throttle_profile,
    )


@upload_bp.route('/process', methods=['POST'])
@login_required
@admin_required
def process_upload():
    """Rota legada: recebe o arquivo completo. O frontend usa chunked upload."""
    _hdr_size = sum(len(k)+len(v)+4 for k,v in _req.headers)
    logger.info(f'[UPLOAD-DIAG] method={_req.method} content_length={_req.content_length} headers_bytes={_hdr_size}')
    form = UploadForm()

    if not form.validate_on_submit():
        errors = []
        for field, errs in form.errors.items():
            errors.extend(errs)
        return jsonify(status='error', message=' | '.join(errors)), 400

    file = form.file.data
    if isinstance(file, list):
        file = file[0] if file else None
    if not file or not hasattr(file, 'filename') or file.filename == '':
        return jsonify(status='error', message='Nenhum arquivo selecionado.'), 400

    job_id = str(uuid.uuid4())
    _set_progress(job_id, 'upload', 'Arquivo recebido. Verificando integridade...', 20)

    zip_folder  = current_app.config['UPLOAD_FOLDER_ZIP']
    os.makedirs(zip_folder, exist_ok=True)

    safe_name    = secure_filename(file.filename) or 'upload.zip'
    timestamp    = datetime.now().strftime('%Y%m%d_%H%M%S')
    cidade_slug  = _storage_slug(current_user.cidade_ativa, 'cidade')
    zip_filename = f"{timestamp}_{cidade_slug}_{safe_name}"
    zip_path     = os.path.join(zip_folder, zip_filename)

    try:
        file.save(zip_path)
    except Exception as e:
        logger.error(f"Erro ao salvar ZIP: {e}")
        _set_progress(job_id, 'error', f'Erro ao salvar arquivo: {e}', 0)
        _expire_job(job_id, 60)
        return jsonify(status='error', message=f'Erro ao salvar arquivo: {e}'), 500

    try:
        periodos = _periodos_upload(_req.form, form.process_current_month.data)
    except ValueError as exc:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return jsonify(status='error', message=str(exc)), 400

    throttle_profile = (_req.form.get('periodo_upload') or '').strip() or None
    return _process_saved_zip(
        zip_path, zip_filename,
        current_user.cidade_ativa, current_user.id,
        form.process_current_month.data, job_id, periodos=periodos,
        throttle_profile=throttle_profile,
    )


def _update_processed_file(zip_filename, cidade, total_links, total_processed,
                           total_skipped, links_mes, pendentes,
                           status, erro, duration=0,
                           links_scrapeados=None, pendencias=None):
    try:
        _ensure_upload_schema()
        pf = ProcessedFile.query.filter_by(
            arquivo_nome=zip_filename, cidade=cidade
        ).first()
        if pf:
            saved_query = Activity.query.filter(
                Activity.processed_file_id == pf.id
            )
            pf.total_atividades      = saved_query.count()
            pf.atividades_validas    = saved_query.filter(
                (Activity.status.is_(None)) | (Activity.status != 'removida')
            ).count()
            pf.status                = status
            pf.mensagem_erro         = erro
            pf.tempo_processamento   = duration
            pf.links_encontrados_mes = links_mes
            pf.atividades_pendentes  = pendentes
            if links_scrapeados is not None:
                pf.links_scrapeados = links_scrapeados
            if pendencias is not None:
                pf.pendencias_json = json.dumps(pendencias, ensure_ascii=False)
            db.session.commit()
    except Exception as e:
        logger.warning(f'Erro ao atualizar ProcessedFile: {e}')
        db.session.rollback()


@upload_bp.errorhandler(413)
def too_large(e):
    return jsonify(status='error', message='Arquivo muito grande. Maximo: 2GB.'), 413
