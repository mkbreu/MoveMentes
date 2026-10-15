# -*- coding: utf-8 -*-
"""Teste que EXECUTA a rota /admin/dashboard de ponta a ponta.

Trava contra o 500 de 15/08 (ver ERROS.md secao 4.8): o card "Publicar o
ranking agora" pedia `pedido_ranking` no template sem garantia nenhuma de
que a rota sempre mandasse a chave. `ast.parse` nao pega esse tipo de erro
-- ele so aparece rodando de verdade, com o Jinja tentando montar a pagina.

NUNCA aponta para o banco de producao. Usa create_app(database_uri=...) com
SQLite em memoria -- e por isso que esse parametro existe em app/__init__.py
(regra 24 do CLAUDE.md: nunca abrir o banco de producao pelo Z:, nem para
ler; mode=ro nao protege, grava -shm mesmo lendo e derruba o worker do NAS).

Rodar: python test_dashboard.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from app import create_app, db
from app.models import Usuario


# create_app() mexe em logging global (FileHandler/StreamHandler) -- chamar
# duas vezes no mesmo processo derruba o segundo handler ("I/O operation on
# closed file"). Uma app so, criada uma vez, para os dois cenarios.
_app = create_app(database_uri='sqlite:///:memory:')
_app.config.update(TESTING=True, WTF_CSRF_ENABLED=False,
                    SERVER_NAME='teste.local')

with _app.app_context():
    db.create_all()
    _usuario = Usuario(nome='Teste', email='teste@teste.com',
                        tipo='master', cidade='CidadeTeste', ativo=True)
    _usuario.set_password('x')
    db.session.add(_usuario)
    db.session.commit()
    _USER_ID = _usuario.id


def _cliente_logado():
    client = _app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(_USER_ID)
        sess['_fresh'] = True
    return client


def testar_dashboard_com_pedido_de_rodada_ok():
    """Caminho feliz: rodada.estado() devolve o dict de sempre."""
    resp = _cliente_logado().get('/admin/dashboard')
    assert resp.status_code == 200, (
        'Dashboard quebrou no caminho feliz: HTTP %s\n%s'
        % (resp.status_code, resp.get_data(as_text=True)[:2000]))
    print('OK: dashboard renderiza no caminho feliz')


def testar_dashboard_sobrevive_a_rodada_estado_quebrado():
    """A REGRESSAO DE 15/08. Se app.rodada.estado() explodir (bug, timeout,
    o que for), a rota tem de continuar mandando pedido_ranking -- e o
    template tem de sobreviver mesmo que, por algum motivo futuro, a chave
    nao chegue. HTTP 200, nunca 500."""
    with patch('app.rodada.estado', side_effect=RuntimeError('simulado')):
        resp = _cliente_logado().get('/admin/dashboard')

    assert resp.status_code == 200, (
        'Dashboard quebrou quando rodada.estado() falhou (era exatamente '
        'o bug do 500 de 15/08): HTTP %s\n%s'
        % (resp.status_code, resp.get_data(as_text=True)[:2000]))
    print('OK: dashboard sobrevive a rodada.estado() quebrado')


if __name__ == '__main__':
    testes = [testar_dashboard_com_pedido_de_rodada_ok,
              testar_dashboard_sobrevive_a_rodada_estado_quebrado]
    falhou = False
    for teste in testes:
        try:
            teste()
        except AssertionError as exc:
            falhou = True
            print('FALHOU: %s -- %s' % (teste.__name__, exc))
        except Exception as exc:
            falhou = True
            print('ERRO INESPERADO: %s -- %s: %s'
                  % (teste.__name__, type(exc).__name__, exc))
    if falhou:
        print('\n--- RESULTADO: FALHOU ---')
        sys.exit(1)
    print('\n--- RESULTADO: TUDO OK ---')
