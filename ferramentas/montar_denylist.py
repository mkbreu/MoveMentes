#!/usr/bin/env python
"""Monta a LISTA LOCAL de dados sensiveis que o `verificar_sensiveis.py` usa.

Le os valores REAIS (pessoas e segredos) de uma COPIA do banco, do cofre e do
.env da automacao, e grava um JSON FORA do repositorio (pasta do usuario).
Nunca imprime um valor -- so contagens.

  python scripts/montar_denylist.py --banco C:\\caminho\\copia.db [--cofre secrets.json] [--env .env]

NUNCA aponte --banco para o banco de producao em Z: (regra 24 do CLAUDE.md):
use a copia feita pelo site (Backups/config_AAAAMMDD_*.db).
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

SAIDA = Path.home() / '.movimente' / 'denylist_sensiveis.json'

# (tabela, coluna, tipo)
COLUNAS = [
    ('atletas', 'nome_atual', 'nomes'), ('atletas', 'nome_exibicao', 'nomes'),
    ('atletas', 'email', 'emails'), ('atletas', 'telefone_normalizado', 'telefones'),
    ('atletas', 'strava_id', 'ids'),
    ('usuarios', 'nome', 'nomes'), ('usuarios', 'email', 'emails'),
    ('usuarios', 'telefone', 'telefones'), ('usuarios', 'senha_provisoria', 'segredos'),
    ('adesoes', 'nome', 'nomes'), ('adesoes', 'email', 'emails'), ('adesoes', 'telefone', 'telefones'),
    ('medalhistas_mes', 'nome', 'nomes'), ('activities', 'nome_usuario', 'nomes'),
    ('activities', 'telefone_whatsapp', 'telefones'),
    ('patrocinador_catalogo', 'nome_interno', 'nomes'), ('patrocinador_catalogo', 'nome_exibicao', 'nomes'),
    ('patrocinador_catalogo', 'telefone', 'telefones'), ('patrocinador_catalogo', 'endereco', 'nomes'),
    ('patrocinadores_mensais', 'patrocinador_nome', 'nomes'), ('patrocinadores_mensais', 'nome_exibicao', 'nomes'),
    ('patrocinadores_mensais', 'telefone', 'telefones'),
    ('sessoes_whatsapp', 'telefone', 'telefones'), ('sessoes_whatsapp', 'email', 'emails'),
    ('config', 'chave_pix', 'segredos'), ('config', 'link_grupo_whatsapp', 'segredos'),
    ('instancias_waha', 'chave', 'segredos'),
    ('local_retirada', 'endereco', 'nomes'), ('local_retirada_por_cidade', 'endereco', 'nomes'),
]


def variantes_telefone(bruto):
    dig = re.sub(r'\D', '', str(bruto))
    saida = set()
    if len(dig) >= 10:
        saida.add(dig[-11:])
        saida.add(dig[-10:])
        if dig.startswith('55') and len(dig) >= 12:
            saida.add(dig)
    return saida


def achatar(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from achatar(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from achatar(v)
    elif isinstance(obj, str):
        yield obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--banco', required=True)
    ap.add_argument('--cofre')
    ap.add_argument('--env', nargs='*', default=[])
    args = ap.parse_args()
    if 'movimente-se' in args.banco.lower() and 'instance' in args.banco.lower() and args.banco.upper().startswith('Z:'):
        sys.exit('RECUSADO: nao abra o banco de producao pelo Z: (regra 24). Use a copia de Backups/.')

    lista = {k: set() for k in ('nomes', 'primeiros', 'emails', 'telefones', 'segredos', 'ids')}
    con = sqlite3.connect(f'file:{args.banco}?mode=ro', uri=True)
    for tabela, coluna, tipo in COLUNAS:
        try:
            for (v,) in con.execute(f'select distinct "{coluna}" from "{tabela}" where "{coluna}" is not null'):
                v = str(v).strip()
                if not v:
                    continue
                if tipo == 'telefones':
                    lista['telefones'] |= variantes_telefone(v)
                elif tipo == 'nomes':
                    limpo = re.sub(r'[^\w\s.-]', '', v, flags=re.UNICODE).strip()
                    if len(limpo.split()) >= 2 or len(limpo) >= 9:
                        lista['nomes'].add(limpo)
                        primeiro = re.sub(r'[^A-Za-zÀ-ÿ]', '', limpo.split()[0])
                        if len(primeiro) >= 5:
                            lista['primeiros'].add(primeiro)
                elif tipo == 'segredos':
                    if len(v) >= 6:
                        lista['segredos'].add(v)
                else:
                    lista[tipo].add(v.lower() if tipo == 'emails' else v)
        except sqlite3.Error as exc:
            print(f'  (pulei {tabela}.{coluna}: {exc})')
    con.close()

    if args.cofre and Path(args.cofre).exists():
        dados = json.loads(Path(args.cofre).read_text(encoding='utf-8-sig'))
        # So valores de configuracoes SENSIVEIS (o resto do cofre e' estado de rotina:
        # carimbos, nomes de modo... e daria falso positivo em palavra comum).
        sensivel = re.compile(r'secret|token|password|senha|key|cookie|client_id|tenant|remetente|'
                              r'wol_mac|numero|destino|grupo|ssh_|cloudflare_|wsl_ip|base_url', re.I)
        for chave, por_escopo in dados.items():
            if not isinstance(por_escopo, dict):
                continue
            for escopo, reg in por_escopo.items():
                if not isinstance(reg, dict):
                    continue
                por = str(reg.get('updated_by') or '')
                if re.fullmatch(r'[^@\s]+@[^@\s]+\.[a-z]{2,}', por, re.I):
                    lista['emails'].add(por.lower())
                valor = reg.get('value')
                if isinstance(valor, str) and len(valor.strip()) >= 6 and sensivel.search(chave):
                    valor = valor.strip()
                    if '@' in valor and ' ' not in valor:
                        lista['emails'].add(valor.lower())
                    lista['segredos'].add(valor)
                    if re.fullmatch(r'[\d\s()+.-]{10,}', valor):
                        lista['telefones'] |= variantes_telefone(valor)
    for env in args.env:
        if Path(env).exists():
            for linha in Path(env).read_text(encoding='utf-8-sig').splitlines():
                if '=' in linha and not linha.strip().startswith('#'):
                    k, v = linha.split('=', 1)
                    v = v.strip().strip('"').strip("'")
                    if re.search(r'PASS|KEY|TOKEN|SECRET|GROUP|CHAT|ENSAIO|URL', k, re.I) and len(v) >= 6                             and not v.lower().startswith(('http://localhost', 'http://127.', 'true', 'false')):
                        lista['segredos'].add(v)
                        if re.fullmatch(r'[\d\s()+.-]{10,}', v):
                            lista['telefones'] |= variantes_telefone(v)

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(json.dumps({k: sorted(v) for k, v in lista.items()}, ensure_ascii=False, indent=0),
                     encoding='utf-8')
    print('lista local gravada em', SAIDA)
    for k, v in lista.items():
        print(f'  {k:10s} {len(v)}')


if __name__ == '__main__':
    main()
