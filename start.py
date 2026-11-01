"""Point d'entrée pour Render : dashboard web (ou mini serveur) + bot Discord."""
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import main  # charge le bot (et le fichier .env)

PORT = int(os.getenv("PORT", "8080"))


class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot en ligne")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # pas de logs pour chaque ping
        pass


def run_simple_server():
    HTTPServer(("0.0.0.0", PORT), Ping).serve_forever()


def run_web():
    try:
        from waitress import serve
        from dashboard.app import create_app

        app = create_app(main.bot)
    except Exception:
        logging.exception("Dashboard indisponible : démarrage d'un serveur minimal à la place")
        run_simple_server()
        return
    logging.info("Dashboard démarré sur le port %s", PORT)
    serve(app, host="0.0.0.0", port=PORT, threads=4)


threading.Thread(target=run_web, daemon=True).start()
main.bot.run(os.getenv("DISCORD_TOKEN"))
