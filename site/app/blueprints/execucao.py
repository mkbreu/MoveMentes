# -*- coding: utf-8 -*-
"""O que o projeto FEZ: ação educativa, acompanhamento nutricional e mídia.

Junta a P-95 (registro de educação e assistência social) e a P-99 (upload de
mídia) numa tela só, porque para quem usa é a mesma pergunta -- *"o que a gente
fez neste mês, e onde está a prova?"*. Separar em duas telas obrigaria a pessoa
a lembrar em qual delas cada coisa mora.

⚠️ NADA AQUI EDITA DADO DE ATLETA. Terceiro autorizado (nutricionista,
colaborador) carrega arquivo e registra o que fez; quilômetro, ranking e
medalha continuam fora do alcance dele.
"""
from datetime import datetime, date
import logging
import os

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    current_app, send_file,
)
from flask_login import login_required, current_user
from app.navegacao import voltar

from app import db
from app import anexos as anexos_mod
from app.models import (
    AcaoEducativa, AcompanhamentoNutricional, MidiaProjeto, Adesao, Usuario,
    TipoAcao,
)
from app.blueprints.auth import (admin_required, master_or_aprovador_required,
                                 master_required, viewer_required)

execucao_bp = Blueprint('execucao', __name__, url_prefix='/admin/execucao')
logger = logging.getLogger(__name__)

CANAIS = (('grupo', 'Grupo de WhatsApp'), ('site', 'Site'),
          ('presencial', 'Presencial'))
SITUACOES = (('em_andamento', 'Em andamento'), ('concluido', 'Concluído'),
             ('desistiu', 'Desistiu'))
TIPOS_MIDIA = (('foto', 'Foto'), ('video', 'Vídeo'), ('documento', 'Documento'))
# 24/09/2026: o projeto deixou de ter só "ação educativa" -- tem evento, pedalada...
# Os campos que um tipo pode pedir (data e nome são sempre pedidos).
CAMPOS_ACAO = (('ministrante', 'Quem organizou / ministrou'), ('canal', 'Canal'),
               ('participantes', 'Alcançou (pessoas)'), ('descricao', 'Descrição'),
               ('material', 'Material / arquivo'))
_TODOS = ','.join(c for c, _ in CAMPOS_ACAO)
# O que toda cidade ganha na primeira vez que abre a tela. Depois o master muda.
TIPOS_INICIAIS = (
    ('Ação educativa', _TODOS),
    ('Evento', 'ministrante,participantes,descricao,material'),
    ('Pedalada', 'ministrante,participantes,descricao,material'),
    ('Corrida', 'ministrante,participantes,descricao,material'),
    ('Caminhada', 'ministrante,participantes,descricao,material'),
    ('Outro', _TODOS),
)
# O que já estava gravado antes dos tipos serem configuráveis.
ROTULO_ANTIGO = {'acao_educativa': 'Ação educativa', 'evento': 'Evento',
                 'pedalada': 'Pedalada', 'outro': 'Outro'}


def _tipos_da_cidade(cidade):
    """Os tipos da cidade; a primeira vez cria os iniciais."""
    tipos = TipoAcao.query.filter_by(cidade=cidade).order_by(TipoAcao.id).all()
    if not tipos:
        for nome, campos in TIPOS_INICIAIS:
            db.session.add(TipoAcao(cidade=cidade, nome=nome, campos=campos))
        db.session.commit()
        tipos = TipoAcao.query.filter_by(cidade=cidade).order_by(TipoAcao.id).all()
    return tipos


def _pasta(sub):
    raiz = str(current_app.config.get('UPLOAD_FOLDER') or 'uploads')
    return os.path.join(raiz, sub)


def _data(bruto):
    try:
        return datetime.strptime((bruto or '').strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _inteiro(bruto):
    try:
        return max(0, int((bruto or '').strip()))
    except (TypeError, ValueError):
        return None


def _valor(bruto):
    texto = (bruto or '').strip().replace('R$', '').replace(' ', '')
    if not texto:
        return 0.0
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    try:
        return abs(round(float(texto), 2))
    except ValueError:
        return 0.0


def _da_cidade(registro):
    """Admin de cidade só mexe no que é da cidade dele."""
    if current_user.is_supremo:
        return True
    return registro.cidade == current_user.cidade_ativa


# ---------------------------------------------------------------------------
# A TELA
# ---------------------------------------------------------------------------
@execucao_bp.route('/')
@login_required
@viewer_required   # 20/09/2026: acompanhar o que o projeto fez e' de todo perfil;
                   # criar acao, anexar midia e apagar seguem restritos abaixo.
def index():
    cidade = current_user.cidade_ativa
    hoje = date.today()
    try:
        ano = int(request.args.get('ano') or hoje.year)
    except (TypeError, ValueError):
        ano = hoje.year

    inicio, fim = date(ano, 1, 1), date(ano + 1, 1, 1)
    acoes = (AcaoEducativa.query
             .filter(AcaoEducativa.cidade == cidade,
                     AcaoEducativa.data >= inicio, AcaoEducativa.data < fim)
             .order_by(AcaoEducativa.data.desc()).all())
    acompanhamentos = (AcompanhamentoNutricional.query
                       .filter_by(cidade=cidade)
                       .order_by(AcompanhamentoNutricional.criado_em.desc()).all())
    midias = (MidiaProjeto.query.filter_by(cidade=cidade)
              .order_by(MidiaProjeto.enviado_em.desc()).all())

    # Quem se inscreveu e marcou condição de saúde -- é o critério 2 do DOC D.
    # ⚠️ A tela mostra que o critério EXISTE, nunca qual é a condição.
    candidatos = (Adesao.query
                  .filter(Adesao.cidade == cidade, Adesao.revogado_em.is_(None))
                  .order_by(Adesao.nome).all())

    # Quem registrou cada ação, e todas as ações da cidade (para ligar uma mídia
    # a um evento de outro ano).
    todas_acoes = (AcaoEducativa.query.filter_by(cidade=cidade)
                   .order_by(AcaoEducativa.data.desc()).limit(300).all())
    ids = {a.criado_por_id for a in acoes if a.criado_por_id}
    autores = ({u.id: u.nome for u in Usuario.query.filter(Usuario.id.in_(ids)).all()}
               if ids else {})

    return render_template(
        'admin/execucao.html',
        todas_acoes=todas_acoes, autores=autores,
        cidade=cidade, ano=ano, hoje=hoje.isoformat(),
        acoes=acoes, acompanhamentos=acompanhamentos, midias=midias,
        candidatos=candidatos,
        canais=CANAIS, situacoes=SITUACOES, tipos_midia=TIPOS_MIDIA,
        tipos_acao=_tipos_da_cidade(cidade), campos_acao=CAMPOS_ACAO,
        rotulo_tipo_acao=ROTULO_ANTIGO,
        total_participantes=sum(a.participantes or 0 for a in acoes),
    )


# ---------------------------------------------------------------------------
# AÇÃO EDUCATIVA
# ---------------------------------------------------------------------------
@execucao_bp.route('/acao', methods=['POST'])
@login_required
@admin_required
def nova_acao():
    quando = _data(request.form.get('data'))
    tema = (request.form.get('tema') or '').strip()
    erros = []
    if not quando:
        erros.append('Informe a data da ação.')
    elif quando > date.today():
        erros.append('Não dá para registrar uma ação no futuro.')
    if len(tema) < 3:
        erros.append('Escreva o tema da ação.')

    material = (None, None, None, 0)
    if not erros:
        try:
            material = anexos_mod.guardar(
                request.files.get('material'), _pasta('educativo'),
                anexos_mod.MATERIAL)
        except anexos_mod.AnexoRecusado as exc:
            erros.append(str(exc))

    if erros:
        for e in erros:
            flash(e, 'error')
        return voltar('execucao.index')

    db.session.add(AcaoEducativa(
        cidade=current_user.cidade_ativa, data=quando, tema=tema[:200],
        tipo=(request.form.get('tipo')
              if request.form.get('tipo') in [t.nome for t in _tipos_da_cidade(
                  current_user.cidade_ativa)] else 'Ação educativa'),
        ministrante=(request.form.get('ministrante') or '').strip()[:160] or None,
        canal=(request.form.get('canal') or '').strip()[:30] or None,
        participantes=_inteiro(request.form.get('participantes')),
        descricao=(request.form.get('descricao') or '').strip()[:4000] or None,
        material_nome=material[0], material_path=material[1], material_mime=material[2],
        criado_por_id=current_user.id,
    ))
    db.session.commit()
    flash('Registrado.', 'success')
    return voltar('execucao.index', ano=quando.year)


@execucao_bp.route('/acao/<int:aid>/material')
@login_required
@admin_required
def material_da_acao(aid):
    """Material sempre como download, nunca inline (regra 28)."""
    acao = AcaoEducativa.query.get_or_404(aid)
    if not _da_cidade(acao):
        abort(403)
    if not acao.material_path or not os.path.exists(acao.material_path):
        abort(404)
    return send_file(acao.material_path, as_attachment=True,
                     download_name=acao.material_nome or 'material')


@execucao_bp.route('/tipo', methods=['POST'])
@login_required
@master_required
def novo_tipo():
    """O master cria um tipo (Corrida, Palestra...) e marca os campos dele."""
    nome = ' '.join((request.form.get('nome') or '').split())[:30]
    campos = [c for c, _ in CAMPOS_ACAO if request.form.get('campo_' + c)]
    cidade = current_user.cidade_ativa
    if len(nome) < 2:
        flash('Dê um nome ao tipo.', 'error')
    elif nome.lower() in [t.nome.lower() for t in _tipos_da_cidade(cidade)]:
        flash('Já existe um tipo com esse nome.', 'error')
    else:
        db.session.add(TipoAcao(cidade=cidade, nome=nome, campos=','.join(campos),
                                criado_por_id=current_user.id))
        db.session.commit()
        flash('Tipo criado.', 'success')
    return redirect(url_for('execucao.index') + '#acoes')


@execucao_bp.route('/tipo/<int:tid>/apagar', methods=['POST'])
@login_required
@master_required
def apagar_tipo(tid):
    """Tira o tipo da lista. O que já foi registrado com ele continua como está."""
    tipo = TipoAcao.query.get_or_404(tid)
    if not _da_cidade(tipo):
        abort(403)
    try:
        from app.backupauto import backup_configuracao
        backup_configuracao()
    except Exception as exc:
        logger.error('backup falhou; NAO vou apagar o tipo %s: %s', tid, exc)
        flash('Não consegui fazer o backup antes de apagar. Nada foi apagado.', 'error')
        return redirect(url_for('execucao.index') + '#acoes')
    logger.warning('tipo de acao %s (%s) apagado por %s', tid, tipo.nome, current_user.email)
    db.session.delete(tipo)
    db.session.commit()
    flash('Tipo apagado.', 'success')
    return redirect(url_for('execucao.index') + '#acoes')


@execucao_bp.route('/acao/<int:aid>/apagar', methods=['POST'])
@login_required
@master_required   # 24/09/2026: quem registra e' o admin; APAGAR e' do master
def apagar_acao(aid):
    """Apaga uma ação/evento -- com backup do banco ANTES (regra 9).

    As fotos, vídeos e documentos ligados a ela NÃO são apagados: ficam soltos
    (sem vínculo) e podem ser ligados a outro evento. Apagar o evento não pode
    levar a prova junto.
    """
    acao = AcaoEducativa.query.get_or_404(aid)
    if not _da_cidade(acao):
        abort(403)
    try:
        from app.backupauto import backup_configuracao
        backup_configuracao()
    except Exception as exc:
        logger.error('backup falhou; NAO vou apagar a acao %s: %s', aid, exc)
        flash('Não consegui fazer o backup antes de apagar. Nada foi apagado.', 'error')
        return voltar('execucao.index')
    soltas = MidiaProjeto.query.filter_by(acao_id=acao.id).all()
    for m in soltas:
        m.acao_id = None
    logger.warning('acao %s (%s, %s) apagada por %s; %d midia(s) ficaram soltas',
                   acao.id, acao.tema, acao.data, current_user.email, len(soltas))
    anexos_mod.apagar(acao.material_path)
    db.session.delete(acao)
    db.session.commit()
    flash('Apagado. Foi feita uma cópia do banco antes.%s' % (
        ' As %d mídia(s) ligadas a ele ficaram sem vínculo.' % len(soltas)
        if soltas else ''), 'success')
    return voltar('execucao.index')


# ---------------------------------------------------------------------------
# ACOMPANHAMENTO NUTRICIONAL
# ---------------------------------------------------------------------------
@execucao_bp.route('/acompanhamento', methods=['POST'])
@login_required
@master_or_aprovador_required
def novo_acompanhamento():
    nome = (request.form.get('beneficiario_nome') or '').strip()
    if len(nome) < 3:
        flash('Escreva o nome de quem vai ser acompanhado.', 'error')
        return voltar('execucao.index')

    inicio = _data(request.form.get('inicio')) or date.today()
    # O pacote é de 3 meses (DOC D). O fim é sugerido, não imposto.
    fim = _data(request.form.get('fim'))
    if not fim:
        ano, mes = inicio.year, inicio.month + 3
        while mes > 12:
            mes -= 12
            ano += 1
        dia = min(inicio.day, 28)
        fim = date(ano, mes, dia)

    db.session.add(AcompanhamentoNutricional(
        cidade=current_user.cidade_ativa,
        beneficiario_nome=nome[:120],
        beneficiario_strava_id=(request.form.get('strava_id') or '').strip()[:50] or None,
        adesao_id=_inteiro(request.form.get('adesao_id')),
        criterio_interesse=request.form.get('criterio_interesse') == 'sim',
        criterio_sorteio=request.form.get('criterio_sorteio') == 'sim',
        criterio_regularidade=request.form.get('criterio_regularidade') == 'sim',
        criterio_observacao=(request.form.get('criterio_observacao') or '').strip()[:2000] or None,
        inicio=inicio, fim=fim,
        profissional=(request.form.get('profissional') or '').strip()[:160] or None,
        custo=_valor(request.form.get('custo')),
        documento_numero=(request.form.get('documento_numero') or '').strip()[:60] or None,
        situacao='em_andamento',
        criado_por_id=current_user.id,
    ))
    db.session.commit()
    flash('Acompanhamento registrado.', 'success')
    return voltar('execucao.index')


@execucao_bp.route('/acompanhamento/<int:aid>/atualizar', methods=['POST'])
@login_required
@master_or_aprovador_required
def atualizar_acompanhamento(aid):
    reg = AcompanhamentoNutricional.query.get_or_404(aid)
    if not _da_cidade(reg):
        abort(403)
    reg.entregou_plano = request.form.get('entregou_plano') == 'sim'
    reg.entregou_cartilha = request.form.get('entregou_cartilha') == 'sim'
    reg.entregou_relatorio = request.form.get('entregou_relatorio') == 'sim'
    situacao = (request.form.get('situacao') or '').strip()
    if situacao in dict(SITUACOES):
        reg.situacao = situacao
    reg.observacao = (request.form.get('observacao') or '').strip()[:2000] or None
    db.session.commit()
    flash('Acompanhamento atualizado.', 'success')
    return voltar('execucao.index')


# ---------------------------------------------------------------------------
# MÍDIA
# ---------------------------------------------------------------------------
@execucao_bp.route('/midia', methods=['POST'])
@login_required
@admin_required
def nova_midia():
    titulo = (request.form.get('titulo') or '').strip()
    tipo = (request.form.get('tipo') or '').strip()
    erros = []
    if len(titulo) < 3:
        erros.append('Dê um título para esta mídia.')
    if tipo not in dict(TIPOS_MIDIA):
        erros.append('Diga se é foto, vídeo ou documento.')

    arquivo = (None, None, None, 0)
    if not erros:
        try:
            arquivo = anexos_mod.guardar(
                request.files.get('arquivo'), _pasta('midia'), anexos_mod.MIDIA)
        except anexos_mod.AnexoRecusado as exc:
            erros.append(str(exc))

    link = (request.form.get('link_youtube') or '').strip()[:300] or None
    if not erros and not arquivo[1] and not link:
        erros.append('Mande o arquivo, ou o link do YouTube.')

    acao_id = _inteiro(request.form.get('acao_id')) or None
    if acao_id:
        acao = AcaoEducativa.query.get(acao_id)
        if not acao or acao.cidade != current_user.cidade_ativa:
            erros.append('O evento escolhido não existe nesta cidade.')

    if erros:
        for e in erros:
            flash(e, 'error')
        return voltar('execucao.index')

    db.session.add(MidiaProjeto(
        cidade=current_user.cidade_ativa, tipo=tipo, titulo=titulo[:200],
        descricao=(request.form.get('descricao') or '').strip()[:4000] or None,
        data=_data(request.form.get('data')) or date.today(),
        arquivo_nome=arquivo[0], arquivo_path=arquivo[1],
        arquivo_mime=arquivo[2], arquivo_bytes=arquivo[3],
        link_youtube=link, acao_id=acao_id,
        # NASCE INTERNA, sempre. Virar pública é um clique deliberado, depois
        # de alguém confirmar o consentimento de quem aparece (P-100).
        publico=False, consentimento_confirmado=False,
        enviado_por_id=current_user.id,
    ))
    db.session.commit()
    flash('Mídia guardada. Ela está INTERNA até alguém confirmar o '
          'consentimento de quem aparece.', 'success')
    return voltar('execucao.index')


@execucao_bp.route('/midia/<int:mid>/liberar', methods=['POST'])
@login_required
@master_or_aprovador_required
def liberar_midia(mid):
    """Confirma o consentimento e, só então, deixa a mídia pública."""
    midia = MidiaProjeto.query.get_or_404(mid)
    if not _da_cidade(midia):
        abort(403)

    midia.consentimento_confirmado = request.form.get('consentimento') == 'sim'
    midia.consentimento_observacao = (
        request.form.get('consentimento_observacao') or '').strip()[:300] or None
    link = (request.form.get('link_youtube') or '').strip()[:300]
    if link:
        midia.link_youtube = link

    quer_publica = request.form.get('publico') == 'sim'
    if quer_publica and not midia.pode_ser_publica:
        if not midia.consentimento_confirmado:
            flash('Sem consentimento de imagem confirmado, esta mídia não pode '
                  'ficar pública. Confirme primeiro.', 'error')
        else:
            flash('Vídeo só fica público com o link do YouTube — o servidor não '
                  'serve vídeo. Publique no YouTube e cole o link aqui.', 'error')
        midia.publico = False
    else:
        midia.publico = quer_publica
        if quer_publica:
            midia.liberado_em = datetime.utcnow()
            midia.liberado_por_id = current_user.id
    db.session.commit()
    return voltar('execucao.index')


@execucao_bp.route('/midia/<int:mid>/arquivo')
@login_required
@admin_required
def arquivo_da_midia(mid):
    midia = MidiaProjeto.query.get_or_404(mid)
    if not _da_cidade(midia):
        abort(403)
    if not midia.arquivo_path or not os.path.exists(midia.arquivo_path):
        abort(404)
    return send_file(midia.arquivo_path, as_attachment=True,
                     download_name=midia.arquivo_nome or 'arquivo')


@execucao_bp.route('/midia/<int:mid>/apagar', methods=['POST'])
@login_required
@master_or_aprovador_required
def apagar_midia(mid):
    """Apaga a mídia -- com backup do banco ANTES (regra 9)."""
    midia = MidiaProjeto.query.get_or_404(mid)
    if not _da_cidade(midia):
        abort(403)
    try:
        from app.backupauto import backup_configuracao
        backup_configuracao()
    except Exception as exc:
        logger.error('backup falhou; NAO vou apagar a midia %s: %s', mid, exc)
        flash('Não consegui fazer o backup antes de apagar. Nada foi apagado.', 'error')
        return voltar('execucao.index')
    anexos_mod.apagar(midia.arquivo_path)
    db.session.delete(midia)
    db.session.commit()
    flash('Mídia apagada. Foi feita uma cópia do banco antes.', 'success')
    return voltar('execucao.index')
