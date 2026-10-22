"""Importa e sincroniza a Base Oficial CAEPI do MTE com o banco de dados da aplicação.

Uso:
    .venv/bin/python popular_caepi.py
"""
import os
import sys

from app import caepi, create_app
from app.db import get_db


def main():
    app = create_app()
    with app.app_context():
        db = get_db()
        print("Iniciando sincronização da Base CAEPI...")
        zips = caepi._caminhos_zip_locais()
        if zips:
            caminho = zips[0]
            print(f"Lendo arquivo oficial local: {caminho}")
            with open(caminho, "rb") as f:
                total, epis = caepi.importar_arquivo_caepi(db, f.read(), caminho)
        else:
            print("Nenhum arquivo .zip local encontrado. Tentando download do FTP oficial do MTE...")
            try:
                total, epis = caepi.sincronizar_base_mte(db)
            except Exception as e:
                print(f"Erro no download via FTP: {e}", file=sys.stderr)
                sys.exit(1)

        print(f"Sucesso! {total} CAs oficiais registrados na base_caepi.")
        print(f"{epis} EPI(s) tiveram sua validade e categoria sincronizadas.")


if __name__ == "__main__":
    main()
