# -*- coding: utf-8 -*-
"""A trava do patrocinador/auditor -- allowlist, no servidor (P-91).

POR QUE ISTO EXISTE, E POR QUE NAO E' NO TEMPLATE
O catalogo de telas (`app/indice.py`) esconde o card de quem nao tem nivel:
`{% if not tela.nivel or current_user|attr(tela.nivel) %}`. Isso esconde o CARD,
nao fecha a ROTA -- quem digitar o endereco entra do mesmo jeito. Para usuario
de dentro isso e' aceitavel (ele ja passou pelos decorators de nivel). Para
usuario EXTERNO, nao: um patrocinador ou um analista do Ministerio nao pode
alcancar a tela de comandos por saber digitar `/admin/comandos`.

⚠️ ALLOWLIST, NUNCA BLOCKLIST. Blocklist esquece a tela que alguem criar amanha
-- e telas nao catalogadas caem em `_SEM_CATEGORIA`, SEM nivel declarado, que e'
exatamente o buraco. Aqui NEGA POR PADRAO: endpoint que nao esta na lista nao
abre, e tela nova precisa ser liberada de proposito.

⚠️ E NAO SE FILTRA O CACHE DO CATALOGO. `indice._cache` e' global, nao por
usuario: filtrar dentro de `construir()` vazaria a arvore do primeiro usuario
para todos. A filtragem fica no template E aqui, nunca no cache.
"""

# Prefixos de endpoint liberados por inteiro (o `.` no fim e' de proposito:
# `conferencia.` libera o blueprint todo, `conferenciaX` nao existe).
PREFIXOS_LIBERADOS = (
    'auth.',          # entrar, sair, trocar a propria senha, trocar de cidade
    'adesao.',        # termos e politica de privacidade -- publicos de qualquer forma
    'conferencia.',   # conferencia publica
    'pedidos.',       # relatorio por link e cadastro pelo atleta (publicos; as telas de gerir exigem admin)
    'static',
)

# Endpoints liberados um a um. Tudo o que nao esta aqui NEGA.
ENDPOINTS_LIBERADOS = frozenset({
    # --- a casa ---
    'index', 'welcome_page', 'favicon',

    # --- os numeros, que e' o que ele veio ver ---
    'ranking.consolidado_publico',
    'ranking.current',
    'ranking.annual',
    'ranking.view_month',
    'ranking.public_links',
    'ranking.public_yearly',
    'ranking.sponsor_card',
    'ranking.public_sponsor_logo',
    'ranking.public_extra_sponsor_logo',
    'ranking.current_sponsor_logo',
    'ranking.catalogo_logo',
    'ranking.download_ranking',
    'ranking.get_ranking_text_json',

    # --- prestacao de contas e transparencia: o coracao da auditoria ---
    'ranking.transparencia_public',
    'ranking.transparencia_comprovante',
    'ranking.comprovante_publico',
    'config.prestacao',
    'config.view_config',

    # --- quem participa, e o que foi entregue ---
    'admin.lista_atletas',
    'admin.medalhistas',
    'admin.fechamento',
    'athlete.dashboard',
    'athlete.todos',

    # --- os relatorios: e' para isto que ele tem acesso (P-98) ---
    'relatorios.index',
    'relatorios.beneficiarios',
    'relatorios.resultados',
    'relatorios.fotografico',
    'relatorios.despesas',

    # --- o livro-caixa em leitura: e' o que ele veio auditar ---
    'caixa.index',
    'caixa.fluxo',
    'caixa.planilha',
    'caixa.comprovante',

    # --- o que o projeto FEZ com o dinheiro (20/09/2026): acao educativa,
    #     acompanhamento e as fotos que os relatorios citam. So leitura: os
    #     formularios de gravar nao aparecem, e as rotas de gravar recusam.
    'execucao.index',
    'execucao.material_da_acao',
    'execucao.arquivo_da_midia',

    # --- o sumario, para ele achar o que pode ver ---
    'admin.indice_telas',
    'admin.dashboard',
    'admin.area',
})

# So para a mensagem de recusa ficar util: o que ele NAO ve, e por que.
_MOTIVO = ('Esta tela é da operação do projeto (automação, mensagens, '
           'usuários e comandos do servidor) e não faz parte do acesso de '
           'auditoria. Os números, a prestação de contas e os relatórios '
           'estão liberados.')


def liberado(endpoint):
    """O endpoint pode ser aberto por um patrocinador/auditor?"""
    if not endpoint:
        return False
    if endpoint in ENDPOINTS_LIBERADOS:
        return True
    return any(endpoint.startswith(p) for p in PREFIXOS_LIBERADOS)


def motivo_da_recusa():
    return _MOTIVO


def instalar(app):
    """Liga a trava no ciclo do request, antes de qualquer view.

    Roda para TODO request, mas sai na primeira linha para quem nao e' auditor
    -- que e' 100% dos acessos de hoje. Custo: uma comparacao de string.
    """
    from flask import request, render_template
    from flask_login import current_user

    @app.before_request
    def _travar_auditor():
        try:
            if not current_user.is_authenticated:
                return None
            if not getattr(current_user, 'is_auditor', False):
                return None
            if liberado(request.endpoint):
                return None
        except Exception:
            # Falha ao decidir NAO pode abrir a porta: na duvida, nega.
            pass
        try:
            return render_template('publico/sem_acesso.html',
                                   motivo=motivo_da_recusa()), 403
        except Exception:
            return motivo_da_recusa(), 403
