#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Script para inicializar o banco de dados e criar usuário master
"""
import os
import sys
from getpass import getpass
from werkzeug.security import generate_password_hash

# Adicionar o diretório do projeto ao path
sys.path.insert(0, '/volume1/web/movimente-se')

def init_database():
    """Inicializa o banco de dados e cria o usuário master"""
    # Importar DEPOIS de ajustar o path
    from app import create_app, db, dadoslocais
    from app.models import Usuario, Config
    
    app = create_app()
    
    with app.app_context():
        print("Criando tabelas do banco de dados...")
        db.create_all()
        print("✅ Tabelas criadas com sucesso!")
        print("-" * 50)
        
        # Verificar se já existe um usuário master
        try:
            master_user = Usuario.query.filter_by(tipo='master').first()
            
            if master_user:
                print(f"✅ Usuário master '{master_user.email}' já existe.")
                print("Banco de dados já inicializado!")
                return
        except Exception as e:
            print(f"⚠️ Verificação de usuário existente falhou: {e}")
            print("Continuando com criação de novo usuário...")
        
        # Criar usuário master inicial
        print("\nCriando usuário master inicial...")
        print("-" * 50)
        
        nome = input("Nome do usuário master: ").strip()
        email = input("Email de login: ").strip()
        
        # Verificar se o email já existe
        try:
            if Usuario.query.filter_by(email=email).first():
                print(f"❌ Email {email} já existe no sistema!")
                return
        except Exception:
            pass
        
        while True:
            senha = getpass("Senha: ")
            confirmacao = getpass("Confirme a senha: ")
            if senha == confirmacao:
                break
            else:
                print("❌ As senhas não coincidem. Tente novamente.")
        
        try:
            # Criar usuário master
            usuario_master = Usuario(
                nome=nome,
                email=email,
                senha_hash=generate_password_hash(senha),
                tipo='master',
                cidade='Sistema',
                ativo=True
            )
            
            db.session.add(usuario_master)
            db.session.commit()
            
            print("✅ Usuário master criado com sucesso!")
            print(f"   Nome: {nome}")
            print(f"   Email: {email}")
            print(f"   Tipo: master")
            print("-" * 50)
            
            # Criar configuração padrão para Palmeirópolis
            config_padrao = Config.query.filter_by(cidade='Palmeirópolis').first()
            
            if not config_padrao:
                config_padrao = Config(
                    cidade='Palmeirópolis',
                    meta_corrida_caminhada=50,
                    meta_ciclismo=200,
                    patrocinador_mes='A definir',
                    chave_pix=dadoslocais.obter('pix_chave_padrao'),
                    qr_pix='qrcode_doacao.png'
                )
                db.session.add(config_padrao)
                db.session.commit()
                print("✅ Configuração padrão criada para Palmeirópolis!")
            else:
                print("✅ Configuração para Palmeirópolis já existe.")
            
            print("\n✅ Banco de dados inicializado com sucesso!")
            print("-" * 50)
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Erro ao criar usuário: {str(e)}")
            import traceback
            traceback.print_exc()
            return

if __name__ == '__main__':
    init_database()