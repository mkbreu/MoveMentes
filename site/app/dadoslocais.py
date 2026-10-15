"""Dados que NAO podem ir para o git: contato, chave PIX, chave legada.

REGRA DO DONO (15/10/2026): nenhum telefone, e-mail, nome de pessoa, senha ou
chave existe no codigo-fonte -- nem em comentario, nem como exemplo. Estes
valores moram em `instance/dados_locais.json` (fora do git: `instance/` e
ignorado) e o codigo so pergunta pelo NOME:

    dadoslocais.obter('contato_email')            # em Python
    {{ dado_local('contato_email') }}             # em template

Arquivo ausente, ilegivel ou sem a chave -> devolve o padrao ('' se nao
houver) e NUNCA levanta excecao: ler um dado de contato nao pode derrubar uma
pagina. O arquivo e' relido sozinho quando muda (compara a data de
modificacao), entao editar o contato nao exige reiniciar o site.

O modelo de todas as chaves esta em `dados_locais.exemplo.json`, na raiz do
repositorio (so com valores de mentira, no formato certo).
"""
import json
from pathlib import Path

_ARQUIVO = Path(__file__).resolve().parents[1] / 'instance' / 'dados_locais.json'
_cache = {'mtime': None, 'dados': {}}


def _carregar():
    try:
        mtime = _ARQUIVO.stat().st_mtime
    except OSError:
        _cache.update(mtime=None, dados={})
        return _cache['dados']
    if mtime != _cache['mtime']:
        try:
            # utf-8-sig: uma marca invisivel de 3 bytes no comeco do arquivo ja
            # derrubou o cofre inteiro (regra 21 do CLAUDE.md).
            dados = json.loads(_ARQUIVO.read_text(encoding='utf-8-sig'))
            _cache.update(mtime=mtime, dados=dados if isinstance(dados, dict) else {})
        except (OSError, ValueError):
            _cache.update(mtime=mtime, dados={})
    return _cache['dados']


def obter(chave, padrao=''):
    """Valor guardado em `instance/dados_locais.json`, ou `padrao`."""
    valor = _carregar().get(chave)
    return padrao if valor is None else valor
