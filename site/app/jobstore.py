# app/jobstore.py
# -*- coding: utf-8 -*-
"""Armazenamento de status de jobs em arquivo, compartilhado entre workers.

O uWSGI roda com mais de um processo; dicionarios em memoria nao sao visiveis
entre eles. Este modulo espelha o estado dos jobs em temp_extract/jobs para que
qualquer worker consiga responder ao polling de status.
"""
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

JOBS_DIR = Path(__file__).resolve().parents[1] / 'temp_extract' / 'jobs'

# 26/08/2026 — o cache da Conferencia (`confcache_*`) morava em JOBS_DIR e
# era lido pelo ticker de retomada (upload._scan_and_resume_jobs) a cada 2
# min junto com trabalho de verdade, numa maquina que ja passa 43% do tempo
# esperando disco (plano `o-ranking-de-manh-ethereal-feather.md`, Passo 6).
# CACHE_DIR e' pasta separada: quem escreve cache usa `base_dir=CACHE_DIR`.
CACHE_DIR = Path(__file__).resolve().parents[1] / 'temp_extract' / 'confcache'

_NAME_RE = re.compile(r'^[a-z_]*[0-9a-f\-]{36}$')


def _path(name, base_dir=None):
    name = str(name or '').strip()
    if not _NAME_RE.match(name):
        return None
    return (base_dir or JOBS_DIR) / f'{name}.json'


def read(name, base_dir=None):
    path = _path(name, base_dir)
    if not path or not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def write(name, payload, base_dir=None):
    path = _path(name, base_dir)
    if not path:
        return
    try:
        (base_dir or JOBS_DIR).mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, ensure_ascii=True)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.debug('Falha ao gravar job %s: %s', name, exc)


def delete(name, base_dir=None):
    path = _path(name, base_dir)
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass
