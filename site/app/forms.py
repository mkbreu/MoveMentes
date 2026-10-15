from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, SubmitField, SelectField, FileField
from wtforms.validators import DataRequired, Email, EqualTo, Length, ValidationError
from flask_wtf.file import FileAllowed # Necessário para FileField

# Validador personalizado para email_validator
try:
    import email_validator
except ImportError:
    email_validator = None

class LoginForm(FlaskForm):
    # Aceita e-mail, nome de usuario OU telefone. Os admins vem do grupo do
    # WhatsApp e lembram do proprio numero muito antes de lembrar do e-mail.
    identifier = StringField('Email, nome de usuário ou telefone', validators=[DataRequired()])

    password = PasswordField('Senha', validators=[DataRequired()])
    remember_me = BooleanField('Lembrar-me')
    submit = SubmitField('Entrar')

class RegistrationForm(FlaskForm):
    nome = StringField('Nome Completo', validators=[DataRequired(), Length(min=3, max=100)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    # Telefone e obrigatorio para TODO usuario (do viewer ao supremo): e por
    # ele que o sistema avisa o responsavel quando algo falha no envio.
    telefone = StringField('Telefone (WhatsApp)', validators=[DataRequired(), Length(min=8, max=30)])
    cidade = SelectField('Cidade', choices=[], validators=[DataRequired()]) # Choices serão populados dinamicamente
    password = PasswordField('Senha', validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirmar Senha', validators=[DataRequired(), EqualTo('password', message='Senhas devem ser iguais')])

    # <<< NOVIDADE: Campos para o uso da RegistrationForm por admins (add/edit user)
    tipo = SelectField('Tipo de Usuário', choices=[ # Preenchido dinamicamente conforme o nivel de quem cria
        ('viewer', 'Visualizador'),
        ('admin', 'Administrador de Cidade'),
        ('aprovador', 'Aprovador'),
        ('master', 'Master da cidade'),
        ('master_supremo', 'Master supremo'),
    ], validators=[DataRequired()])
    ativo = BooleanField('Usuário Ativo')

    submit = SubmitField('Registrar') # O texto do botão pode mudar no template se for para "Adicionar Usuário"

class UploadForm(FlaskForm):
    file = FileField('Arquivo ZIP ou TXT do WhatsApp', validators=[
        DataRequired(),
        FileAllowed(['zip', 'txt'], 'Apenas arquivos ZIP ou TXT são permitidos!')
    ])
    process_current_month = BooleanField('Processar apenas atividades do mês atual (recomendado)', default=True)
    submit = SubmitField('Processar Upload')

