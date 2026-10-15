# -*- coding: utf-8 -*-
"""Os documentos que o Ministério pede (P-98).

⚠️ POR QUE NÃO HÁ BIBLIOTECA DE PDF AQUI
Conferido no servidor em 28/08/2026: **nenhuma** existe instalada (nem
reportlab, nem weasyprint, nem fpdf, nem PIL) e sobram **243 MB de RAM** no
DS215j. Instalar uma para gerar documento sob demanda significaria: dependência
nova em produção, geração em segundo plano com prazo, e o risco que o P-80 já
mostrou -- parse de HTML pesado travando o worker.

A saída é melhor e não custa nada: **páginas feitas para imprimir**. O
navegador de quem lê já sabe salvar como PDF, em qualquer telefone ou
computador, com fonte melhor do que a que a gente geraria. Zero dependência,
zero RAM, zero trabalho em segundo plano.

Se um dia o Ministério exigir PDF gerado no servidor, o caminho já está pronto:
o HTML aqui é o modelo, e só faltaria a biblioteca.

OS QUATRO DOCUMENTOS (DOC B §8, via DOC E):
  1. lista de beneficiários -- exigência literal;
  2. resultados por período, com indicadores numéricos;
  3. relatório fotográfico datado;
  4. despesas por rubrica do orçamento.
"""
from datetime import date, datetime
import logging

from sqlalchemy import func

from flask import Blueprint, render_template, request, abort
from flask_login import login_required, current_user

from app import db
from app import caixa as caixa_mod
from app.models import (
    Activity, Atleta, Adesao, AcaoEducativa, AcompanhamentoNutricional,
    MidiaProjeto, LancamentoCaixa,
)
from app.blueprints.auth import viewer_required

relatorios_bp = Blueprint('relatorios', __name__, url_prefix='/admin/relatorios')
logger = logging.getLogger(__name__)

MESES = ('', 'janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
         'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro')


def _periodo():
    """De/até vindos da URL; o padrão é o ano corrente."""
    hoje = date.today()
    try:
        de = datetime.strptime(request.args.get('de', ''), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        de = date(hoje.year, 1, 1)
    try:
        ate = datetime.strptime(request.args.get('ate', ''), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        ate = hoje
    if de > ate:
        de, ate = ate, de
    return de, ate


def _indicadores(cidade, de, ate):
    """Os números que o Ministério pede -- todos contáveis, nenhum adjetivo."""
    from app.consolidado import _base_valida

    filtros = _base_valida() + [
        Activity.cidade == cidade,
        Activity.data_hora >= datetime.combine(de, datetime.min.time()),
        Activity.data_hora <= datetime.combine(ate, datetime.max.time()),
    ]
    km, atividades = db.session.query(
        func.coalesce(func.sum(Activity.distancia), 0.0), func.count()
    ).filter(*filtros).one()
    ativos = db.session.query(
        func.count(func.distinct(Activity.id_usuario_strava))
    ).filter(*filtros).scalar() or 0

    acoes = AcaoEducativa.query.filter(
        AcaoEducativa.cidade == cidade,
        AcaoEducativa.data >= de, AcaoEducativa.data <= ate).all()

    acomp = AcompanhamentoNutricional.query.filter(
        AcompanhamentoNutricional.cidade == cidade).all()
    concluidos = [a for a in acomp if a.situacao == 'concluido']

    inscritos = Adesao.query.filter(
        Adesao.cidade == cidade, Adesao.revogado_em.is_(None)).count()

    contas = caixa_mod.periodo(cidade, de, ate)

    return {
        'km': round(km or 0, 1),
        'atividades': atividades or 0,
        'participantes_ativos': ativos,
        'inscritos': inscritos,
        'acoes_educativas': len(acoes),
        'pessoas_alcancadas': sum(a.participantes or 0 for a in acoes),
        'acompanhamentos': len(acomp),
        'acompanhamentos_concluidos': len(concluidos),
        'investido': contas['saidas'],
        'apoio_recebido': contas['entradas'],
        'caixa': contas,
    }


@relatorios_bp.route('/')
@login_required
@viewer_required
def index():
    """A escolha do documento e do período."""
    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    return render_template(
        'admin/relatorios.html',
        cidade=cidade, de=de, ate=ate,
        ind=_indicadores(cidade, de, ate),
    )


@relatorios_bp.route('/beneficiarios')
@login_required
@viewer_required
def beneficiarios():
    """Exigência literal do Ministério: quem foi beneficiado.

    ⚠️ SEM DADO SENSÍVEL. Este documento sai do projeto: não leva condição de
    saúde, não leva telefone e não leva e-mail. Leva nome, cidade e o que a
    pessoa fez -- que é o que se pede.

    ⚠️ E RESPEITA QUEM PEDIU RESERVA: quem marcou "não quero aparecer" entra
    como "Participante reservado", com os números preservados. A pessoa pediu
    anonimato, não exclusão da contagem.
    """
    from app.consolidado import _base_valida, ocultos

    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    reservados = ocultos()

    filtros = _base_valida() + [
        Activity.cidade == cidade,
        Activity.data_hora >= datetime.combine(de, datetime.min.time()),
        Activity.data_hora <= datetime.combine(ate, datetime.max.time()),
    ]
    linhas = []
    consulta = db.session.query(
        Activity.id_usuario_strava,
        func.max(Activity.nome_usuario),
        func.coalesce(func.sum(Activity.distancia), 0.0),
        func.count(),
        func.min(Activity.data_hora),
    ).filter(*filtros).group_by(Activity.id_usuario_strava).order_by(
        func.max(Activity.nome_usuario))

    for sid, nome, km, qtd, desde in consulta.all():
        reservado = str(sid or '') in reservados
        linhas.append({
            'nome': 'Participante reservado' if reservado else ' '.join((nome or '').split()),
            'reservado': reservado,
            'km': round(km or 0, 1),
            'atividades': qtd,
            'desde': desde,
        })

    return render_template(
        'admin/relatorio_beneficiarios.html',
        cidade=cidade, de=de, ate=ate, linhas=linhas,
        total_km=round(sum(l['km'] for l in linhas), 1),
        gerado_em=datetime.now(),
    )


@relatorios_bp.route('/resultados')
@login_required
@viewer_required
def resultados():
    """Indicadores numéricos do período, e o que os produziu."""
    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    acoes = AcaoEducativa.query.filter(
        AcaoEducativa.cidade == cidade,
        AcaoEducativa.data >= de, AcaoEducativa.data <= ate
    ).order_by(AcaoEducativa.data).all()
    acomp = AcompanhamentoNutricional.query.filter_by(
        cidade=cidade).order_by(AcompanhamentoNutricional.inicio).all()

    return render_template(
        'admin/relatorio_resultados.html',
        cidade=cidade, de=de, ate=ate,
        ind=_indicadores(cidade, de, ate),
        acoes=acoes, acompanhamentos=acomp,
        rotulo_tipo_acao={'acao_educativa': 'Ação educativa', 'evento': 'Evento',
                          'pedalada': 'Pedalada', 'outro': 'Outro'},
        gerado_em=datetime.now(),
    )


@relatorios_bp.route('/fotografico')
@login_required
@viewer_required
def fotografico():
    """Relatório fotográfico datado: fotos embutidas, vídeos listados.

    ⚠️ SÓ ENTRA MÍDIA COM CONSENTIMENTO CONFIRMADO. Este documento sai do
    projeto; imagem de quem não autorizou não pode ir junto (P-100).
    """
    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    midias = MidiaProjeto.query.filter(
        MidiaProjeto.cidade == cidade,
        MidiaProjeto.consentimento_confirmado.is_(True),
        MidiaProjeto.data >= de, MidiaProjeto.data <= ate,
    ).order_by(MidiaProjeto.data).all()

    grupos = {}
    for m in midias:
        if m.tipo != 'foto':
            continue
        chave = m.acao_id or 0
        grupo = grupos.setdefault(chave, {
            'titulo': ('%s — %s' % (m.acao.tema, m.acao.data.strftime('%d/%m/%Y'))
                       if m.acao else 'Sem evento vinculado'),
            'ordem': (m.acao.data.toordinal() if m.acao else 10 ** 9),
            'fotos': []})
        grupo['fotos'].append(m)
    grupos_de_fotos = sorted(grupos.values(), key=lambda g: g['ordem'])

    return render_template(
        'admin/relatorio_fotografico.html',
        cidade=cidade, de=de, ate=ate,
        grupos_de_fotos=grupos_de_fotos,
        fotos=[m for m in midias if m.tipo == 'foto'],
        videos=[m for m in midias if m.tipo == 'video' and m.link_youtube],
        sem_consentimento=MidiaProjeto.query.filter(
            MidiaProjeto.cidade == cidade,
            MidiaProjeto.consentimento_confirmado.is_(False)).count(),
        gerado_em=datetime.now(),
    )


@relatorios_bp.route('/despesas')
@login_required
@viewer_required
def despesas():
    """Despesa por rubrica do orçamento -- o vínculo que a LIE exige."""
    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    linhas = LancamentoCaixa.query.filter(
        LancamentoCaixa.cidade == cidade,
        LancamentoCaixa.data >= de, LancamentoCaixa.data <= ate,
    ).order_by(LancamentoCaixa.data).all()

    caixa_mod.garantir_tokens(linhas)
    por_rubrica = {}
    for l in linhas:
        if l.tipo != 'saida':
            continue
        chave = ((l.rubrica or '').strip()
                 or caixa_mod.ROTULO_CATEGORIA.get(l.categoria, l.categoria))
        item = por_rubrica.setdefault(chave, {'rubrica': chave, 'total': 0.0, 'linhas': []})
        item['total'] = round(item['total'] + (l.valor or 0), 2)
        item['linhas'].append(l)

    return render_template(
        'admin/relatorio_despesas.html',
        cidade=cidade, de=de, ate=ate,
        grupos=sorted(por_rubrica.values(), key=lambda x: x['total'], reverse=True),
        entradas=[l for l in linhas if l.tipo == 'entrada'],
        rotulo_categoria=caixa_mod.ROTULO_CATEGORIA,
        documento=caixa_mod.rotulo_documento,
        rotulo_comprovante=caixa_mod.rotulo_comprovante,
        link_publico=lambda l: caixa_mod.url_publica_comprovante(l, 'https://%s' % request.host),
        periodo=caixa_mod.periodo(cidade, de, ate),
        gerado_em=datetime.now(),
    )
