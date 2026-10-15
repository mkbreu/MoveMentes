# -*- coding: utf-8 -*-
"""Gerador de planilha .xlsx SEM biblioteca nenhuma -- só a biblioteca padrão.

POR QUE NÃO `openpyxl`: o NAS tem 490 MB e ~80 MB livres, e a biblioteca nem está
instalada lá. Um .xlsx é um zip de alguns XML; para uma tabela com cabeçalho
colorido, borda, filtro e links, escrevê-los à mão custa ~150 linhas e zero
memória extra. Serve para o livro-caixa (centenas de linhas, nunca milhares).

O QUE ENTREGA (o que o CSV cru não consegue): cabeçalho escuro em negrito,
linhas alternadas, bordas, filtro nas colunas, primeira linha da tabela
congelada, largura de coluna ajustada, data como DATA, valor como MOEDA (saída
em vermelho, com sinal) e link clicável para o comprovante.
"""
import io
import re
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

# Tipos de coluna aceitos em `colunas[i]['tipo']`.
TEXTO, DATA, MOEDA, LINK, NUMERO = 'texto', 'data', 'moeda', 'link', 'numero'

_ILEGAIS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

# Índices dos estilos (cellXfs) declarados em _ESTILOS. Par/ímpar = linha
# alternada: o índice "alt" é sempre o do normal + 1.
_S_CABECALHO = 1
_S_TEXTO, _S_DATA, _S_MOEDA, _S_LINK, _S_NUMERO = 2, 4, 6, 8, 10
_S_TOTAL_ROTULO, _S_TOTAL_MOEDA = 12, 13
_S_TITULO, _S_SUBTITULO = 14, 15

_ESTILOS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2">
<numFmt numFmtId="164" formatCode="dd/mm/yyyy"/>
<numFmt numFmtId="165" formatCode="&quot;R$&quot;\\ #,##0.00;[Red]\\-&quot;R$&quot;\\ #,##0.00"/>
</numFmts>
<fonts count="5">
<font><sz val="10"/><name val="Calibri"/></font>
<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
<font><u/><sz val="10"/><color rgb="FF0563C1"/><name val="Calibri"/></font>
<font><b/><sz val="10"/><name val="Calibri"/></font>
<font><b/><sz val="14"/><color rgb="FF2F4A1F"/><name val="Calibri"/></font>
</fonts>
<fills count="5">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF2F4A1F"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFEEF3E8"/><bgColor indexed="64"/></patternFill></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFD9E4CC"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border>
<left style="thin"><color rgb="FFB7C4A6"/></left><right style="thin"><color rgb="FFB7C4A6"/></right>
<top style="thin"><color rgb="FFB7C4A6"/></top><bottom style="thin"><color rgb="FFB7C4A6"/></bottom><diagonal/>
</border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="16">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf numFmtId="164" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="top"/></xf>
<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="165" fontId="0" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="top"/></xf>
<xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment vertical="top"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="top"/></xf>
<xf numFmtId="0" fontId="3" fillId="4" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="165" fontId="3" fillId="4" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="right" vertical="center"/></xf>
<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _limpo(texto):
    return escape(_ILEGAIS.sub('', str(texto)))


def _col(n):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letras = ''
    n += 1
    while n:
        n, resto = divmod(n - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def _serial(d):
    """Data -> número de série do Excel (dias desde 30/12/1899)."""
    if isinstance(d, datetime):
        d = d.date()
    return (d - date(1899, 12, 30)).days


def _celula(ref, tipo, valor, alt):
    """Uma célula do corpo da tabela. `alt` = linha de fundo colorido."""
    off = 1 if alt else 0
    if valor is None or valor == '':
        base = {DATA: _S_DATA, MOEDA: _S_MOEDA, LINK: _S_LINK,
                NUMERO: _S_NUMERO}.get(tipo, _S_TEXTO)
        return '<c r="%s" s="%d"/>' % (ref, base + off)
    if tipo == DATA:
        return '<c r="%s" s="%d"><v>%d</v></c>' % (ref, _S_DATA + off, _serial(valor))
    if tipo == MOEDA:
        return '<c r="%s" s="%d"><v>%.2f</v></c>' % (ref, _S_MOEDA + off, float(valor))
    if tipo == NUMERO:
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, _S_NUMERO + off, valor)
    if tipo == LINK:
        # (texto, url). HYPERLINK() em vez de relação externa: o mesmo efeito,
        # sem um arquivo a mais dentro do zip.
        texto, url = valor
        formula = 'HYPERLINK("%s","%s")' % (
            str(url).replace('"', '""'), str(texto).replace('"', '""'))
        return '<c r="%s" s="%d" t="str"><f>%s</f><v>%s</v></c>' % (
            ref, _S_LINK + off, _limpo(formula), _limpo(texto))
    return '<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (
        ref, _S_TEXTO + off, _limpo(valor))


def gerar_xlsx(nome_aba, titulo, subtitulo, colunas, linhas, totais=None):
    """Devolve os bytes de um .xlsx.

    colunas: [{'titulo': str, 'largura': int, 'tipo': TEXTO|DATA|MOEDA|LINK|NUMERO}]
    linhas:  lista de listas, na ordem das colunas. LINK = (texto, url) ou None.
    totais:  [(rotulo, valor_float)] -- vão abaixo da tabela, rótulo em negrito.
    """
    n_col = len(colunas)
    ultima = _col(n_col - 1)
    linhas_xml = []

    linhas_xml.append('<row r="1" ht="24" customHeight="1"><c r="A1" s="%d" t="inlineStr">'
                      '<is><t>%s</t></is></c></row>' % (_S_TITULO, _limpo(titulo)))
    linhas_xml.append('<row r="2"><c r="A2" s="%d" t="inlineStr"><is><t>%s</t></is></c></row>'
                      % (_S_SUBTITULO, _limpo(subtitulo)))

    cab = 4
    celulas = ''.join(
        '<c r="%s%d" s="%d" t="inlineStr"><is><t>%s</t></is></c>'
        % (_col(i), cab, _S_CABECALHO, _limpo(c['titulo']))
        for i, c in enumerate(colunas))
    linhas_xml.append('<row r="%d" ht="30" customHeight="1">%s</row>' % (cab, celulas))

    for k, linha in enumerate(linhas):
        r = cab + 1 + k
        alt = (k % 2 == 1)
        celulas = ''.join(
            _celula('%s%d' % (_col(i), r), colunas[i]['tipo'],
                    linha[i] if i < len(linha) else None, alt)
            for i in range(n_col))
        linhas_xml.append('<row r="%d">%s</row>' % (r, celulas))

    fim_dados = cab + len(linhas)
    r = fim_dados + 2
    col_valor = next((i for i, c in enumerate(colunas) if c['tipo'] == MOEDA), n_col - 1)
    for rotulo, valor in (totais or []):
        linhas_xml.append(
            '<row r="%d"><c r="%s%d" s="%d" t="inlineStr"><is><t>%s</t></is></c>'
            '<c r="%s%d" s="%d"><v>%.2f</v></c></row>'
            % (r, _col(col_valor - 1), r, _S_TOTAL_ROTULO, _limpo(rotulo),
               _col(col_valor), r, _S_TOTAL_MOEDA, float(valor)))
        r += 1

    larguras = ''.join(
        '<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, c.get('largura', 16))
        for i, c in enumerate(colunas))

    filtro = ('<autoFilter ref="A%d:%s%d"/>' % (cab, ultima, max(fim_dados, cab))
              if linhas else '')
    folha = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane ySplit="%d" topLeftCell="A%d" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15"/>'
        '<cols>%s</cols><sheetData>%s</sheetData>%s'
        '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
        '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        '</worksheet>'
    ) % (cab, cab + 1, larguras, ''.join(linhas_xml), filtro)

    nome_aba = _ILEGAIS.sub('', re.sub(r'[\[\]\*\?/\\:]', ' ', nome_aba))[:31] or 'Planilha'
    livro = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets>%s</workbook>'
    ) % (escape(nome_aba), (
        '<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
        "'%s'!$A$%d:$%s$%d</definedName></definedNames>"
        % (escape(nome_aba.replace("'", "''")), cab, ultima, max(fim_dados, cab))) if linhas else '')

    tipos = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>')
    rels_raiz = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>')
    rels_livro = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>')

    saida = io.BytesIO()
    with zipfile.ZipFile(saida, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', tipos)
        z.writestr('_rels/.rels', rels_raiz)
        z.writestr('xl/workbook.xml', livro)
        z.writestr('xl/_rels/workbook.xml.rels', rels_livro)
        z.writestr('xl/styles.xml', _ESTILOS)
        z.writestr('xl/worksheets/sheet1.xml', folha)
    return saida.getvalue()
