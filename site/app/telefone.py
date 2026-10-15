# -*- coding: utf-8 -*-
"""Um jeito so de tratar telefone no sistema inteiro.

POR QUE ESTE MODULO EXISTE
O telefone era digitado de tres jeitos diferentes e guardado do jeito que
vinha. "(DD) 9XXXX-XXXX" e "DD9XXXXXXXX" viravam duas coisas distintas: a
legenda do patrocinador saia bonita para um e crua para o outro, e a fila de
mensagens tentava mandar para um destino com parenteses — que o WhatsApp
recusa.

Aqui existem duas formas e so duas:

  normalizar()  ->  '55DD9XXXXXXXX'   o que a maquina usa (WAHA, fila, login)
  bonito()      ->  '(DD) 9XXXX-XXXX' o que a pessoa le (legenda, tela)

Guardar cru e mostrar bonito. Nunca o contrario.
"""
import re

_SO_DIGITOS = re.compile(r'\D')


def normalizar(bruto):
    """Qualquer jeito de digitar vira 55DD9XXXXXXXX, ou None.

    Devolve None quando nao da para confiar no numero. Preferimos nao enviar
    a enviar para um estranho.
    """
    numero = _SO_DIGITOS.sub('', str(bruto or ''))
    if not numero:
        return None
    numero = numero.lstrip('0')
    if not numero.startswith('55'):
        numero = '55' + numero
    if len(numero) == 12:
        # Celular antigo sem o 9 na frente. Fixo comeca com 2..5 e fica como esta.
        ddd, resto = numero[2:4], numero[4:]
        if resto[:1] in ('6', '7', '8', '9'):
            numero = '55' + ddd + '9' + resto
    return numero if len(numero) == 13 else None


# O que aconteceu com o numero quando ele foi normalizado.
JA_CANONICO = 'ja_canonico'
SO_FORMATO = 'so_formato'      # tirou +, espaco, parenteses; completou o 55
INSERIU_NONO = 'inseriu_nono'  # acrescentou o 9 depois do DDD
NAO_ENTENDI = 'nao_entendi'
VAZIO = 'vazio'

_ROTULO_MUDANCA = {
    JA_CANONICO: 'Já está no formato certo',
    SO_FORMATO: 'Só tirou símbolos / completou o 55',
    INSERIU_NONO: 'Acrescentou o 9 depois do DDD',
    NAO_ENTENDI: 'Não consegui entender',
    VAZIO: 'Sem telefone',
}


def analisar(bruto):
    """{original, canonico, mudanca, rotulo, seguro} — sem tocar em banco.

    POR QUE SEPARAR "SEGURO" DE "MUDOU"
    Tirar o `+` e completar o `55` nao inventa nada: o numero continua sendo o
    mesmo, so escrito direito. Acrescentar o **nono digito** e outra conversa —
    e mexer no numero de outra pessoa, e numero errado manda mensagem para
    estranho (regra 22-B).

    Mesmo assim, o nono digito NAO e chute quando o numero e de celular: o plano
    nacional acrescentou o 9 a todos os celulares, e celular antigo comecava com
    6, 7, 8 ou 9 — fixo comeca com 2 a 5. Entao para 6-9 a conta e determinada,
    nao adivinhada; para o resto, `normalizar` nao mexe e aqui nao se finge que
    entendeu.

    `seguro=True` significa "pode aplicar em lote". `seguro=False` significa
    "isto passa por gente antes".
    """
    original = str(bruto or '').strip()
    if not original:
        return {'original': original, 'canonico': None, 'mudanca': VAZIO,
                'rotulo': _ROTULO_MUDANCA[VAZIO], 'seguro': False}

    canonico = normalizar(original)
    if not canonico:
        return {'original': original, 'canonico': None, 'mudanca': NAO_ENTENDI,
                'rotulo': _ROTULO_MUDANCA[NAO_ENTENDI], 'seguro': False}

    if original == canonico:
        mudanca = JA_CANONICO
    else:
        # Se os digitos que ja estavam la sobreviveram inteiros, nada foi
        # inventado — so caiu simbolo ou entrou o 55.
        digitos = _SO_DIGITOS.sub('', original).lstrip('0')
        sem_55 = digitos[2:] if digitos.startswith('55') else digitos
        mudanca = SO_FORMATO if len(sem_55) == 11 else INSERIU_NONO

    return {'original': original, 'canonico': canonico, 'mudanca': mudanca,
            'rotulo': _ROTULO_MUDANCA[mudanca],
            'seguro': mudanca in (JA_CANONICO, SO_FORMATO, INSERIU_NONO)}


def mesmo_numero(a, b):
    """Os dois telefones sao da mesma pessoa? (regra 22-D)

    Compara pais+DDD e os 8 ULTIMOS digitos. O WAHA devolve o numero da sessao
    SEM o nono digito (55DDXXXXXXXX) e o cadastro guarda COM ele
    (55DD9XXXXXXXX): comparar cru concluiria que sao gente diferente — e foi
    assim que um alerta quase foi para o proprio numero que o enviava, o que
    devolve HTTP 500 (regra 20).
    """
    a = _SO_DIGITOS.sub('', str(a or ''))
    b = _SO_DIGITOS.sub('', str(b or ''))
    if len(a) < 10 or len(b) < 10:
        return False
    return (a[:4], a[-8:]) == (b[:4], b[-8:])


def bonito(bruto):
    """'55DD9XXXXXXXX' -> '(DD) 9XXXX-XXXX'. O que a pessoa le.

    Se o numero nao fizer sentido, devolve o que veio (sem inventar nem
    esconder): melhor o admin ver o que ele digitou errado do que ver vazio.
    """
    original = str(bruto or '').strip()
    numero = normalizar(original)
    if not numero:
        return original
    ddd = numero[2:4]
    corpo = numero[4:]
    if len(corpo) == 9:
        return '({}) {}-{}'.format(ddd, corpo[:5], corpo[5:])
    return '({}) {}-{}'.format(ddd, corpo[:4], corpo[4:])


def whatsapp_url(bruto):
    """Link wa.me pronto, ou None."""
    numero = normalizar(bruto)
    return ('https://wa.me/' + numero) if numero else None
