"""Ponto de entrada. Uso:  python run.py   (ou: python run.py --porta 8000)."""
import argparse

from app import create_app

app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Controle de Estoque de EPIs")
    parser.add_argument("--host", default="0.0.0.0",
                        help="0.0.0.0 libera acesso pela rede interna (padrão); use 127.0.0.1 para só este PC")
    parser.add_argument("--porta", type=int, default=5000)
    args = parser.parse_args()
    try:
        from waitress import serve  # servidor de produção, se instalado
        serve(app, host=args.host, port=args.porta)
    except ImportError:
        app.run(host=args.host, port=args.porta, debug=False)
