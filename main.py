#!/usr/bin/env python3
"""Esnaf Randevu Asistanı — tek komutla çalışan giriş noktası.

Kullanım
--------
    python main.py                 # paneli + otomatik motoru başlatır
    python main.py --demo          # örnek verilerle başlatır
    python main.py --tick-once     # motoru bir kez çalıştırıp çıkar (cron için)
    python main.py --port 8080 --no-browser

Sunucu varsayılan olarak yalnızca 127.0.0.1'e bağlanır; tek kullanıcılı,
yerel bir paneldir.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import webbrowser

import config

logger = logging.getLogger("esnaf.main")


def _force_utf8_stdio() -> None:
    """Windows'ta `python main.py > log.txt` gibi yönlendirmelerde Unicode hatasını önler."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Yerel esnaflar için WhatsApp randevu hatırlatıcı ve Google yorum paneli.",
    )
    parser.add_argument("--host", default=config.host(), help="Arayüz adresi (varsayılan 127.0.0.1)")
    parser.add_argument("--port", type=int, default=config.port(), help="Port (varsayılan 5000)")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Panelin dolu görünmesi için örnek müşteri ve randevular ekler",
    )
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="Otomatik mesaj motorunu başlatmadan yalnızca paneli açar",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Başlangıçta tarayıcıyı otomatik açma",
    )
    parser.add_argument(
        "--tick-once",
        action="store_true",
        help="Zamanı gelen mesajları bir kez işleyip çıkar (Windows Görev Zamanlayıcı / cron için)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Ayrıntılı günlük kaydı (Flask hata ayıklayıcı kapalı kalır)",
    )
    return parser


def _print_banner(
    host: str, port: int, provider: str, worker_running: bool, external: bool = False
) -> None:
    base = f"http://{host}:{port}"
    if worker_running:
        worker_note = "çalışıyor"
    elif external:
        worker_note = "harici scheduler sürecinde çalışıyor"
    else:
        worker_note = "kapalı"
    lines = [
        "",
        "  ╭──────────────────────────────────────────────────────────────╮",
        "  │  ESNAF RANDEVU ASİSTANI                                      │",
        "  │  WhatsApp hatırlatma + Google yorum toplama (yerel MVP)      │",
        "  ╰──────────────────────────────────────────────────────────────╯",
        "",
        f"    Panel        : {base}",
        f"    Ayarlar      : {base}/ayarlar",
        f"    Durum        : {base}/api/health",
        f"    WhatsApp     : {provider}" + ("  (gerçek gönderim yok)" if provider == "simulation" else ""),
        f"    Otomatik motor: {worker_note}",
        "",
        "    Durdurmak için: Ctrl+C",
        "",
    ]
    if not config.is_loopback(host):
        lines.insert(
            -1,
            "    UYARI: Panel yerel ağa açılıyor (parola korumalı, bağlantı şifresiz HTTP).\n"
            "           Yalnızca güvendiğiniz ağda kullanın ve ESNAF_TRUSTED_HOSTS ayarlayın.",
        )
    print("\n".join(lines), flush=True)


def _serve(application, host: str, port: int) -> None:
    """Üretim için waitress kullanır; kurulu değilse Flask'ın geliştirme sunucusuna düşer."""
    try:
        from waitress import serve
    except ImportError:
        logger.warning(
            "waitress kurulu değil; Flask geliştirme sunucusu kullanılıyor. "
            "Kalıcı kullanım için: pip install -r requirements.txt"
        )
        application.run(
            host=host,
            port=port,
            debug=False,  # Werkzeug debugger (PIN/RCE yüzeyi) asla açılmaz
            use_reloader=False,  # motorun iki kez başlamasını engeller
            threaded=True,
        )
        return
    serve(application, host=host, port=port, threads=8, ident="esnaf-panel")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
    config.load_dotenv()
    config.ensure_dirs()
    config.setup_logging(logging.DEBUG if args.debug else logging.INFO)

    import db  # noqa: E402  (yollar .env'den sonra çözülsün)
    import scheduler  # noqa: E402
    import utils  # noqa: E402
    import whatsapp  # noqa: E402

    db.init_db()

    if args.demo:
        created = db.seed_demo()
        if created:
            print(f"  ✓ {created} örnek randevu eklendi (demo).", flush=True)
        else:
            print("  • Veritabanında zaten veri var; demo verisi eklenmedi.", flush=True)

    if args.tick_once:
        # Zamanlanmış görevde ayarlardaki "otomatik gönderim kapalı" anahtarına uyulur.
        summary = scheduler.run_once(respect_enabled=True)
        if summary.get("disabled"):
            print(
                "  • Otomatik gönderim ayarlardan kapalı (Ayarlar → Otomatik gönderim).",
                flush=True,
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0

    try:
        from app import create_app  # noqa: E402
    except ImportError as exc:  # Flask kurulu değilse anlaşılır mesaj
        print(
            "\n  HATA: Gerekli Python paketleri kurulu değil.\n"
            f"  Ayrıntı: {exc}\n\n"
            "  Çözüm:  pip install -r requirements.txt\n",
            flush=True,
        )
        return 1

    if not config.is_loopback(args.host) and not config.admin_password():
        print(
            f"\n  HATA: Panel {args.host} adresinde yerel ağa açılmak isteniyor ama parola yok.\n"
            "  Kimlik doğrulaması olmadan ağdaki herkes müşteri verilerine erişebilir.\n\n"
            "  Çözüm: .env dosyasına ESNAF_PASSWORD=guclu-bir-parola ekleyin\n"
            "         veya yalnızca bu bilgisayardan kullanın: python main.py\n",
            flush=True,
        )
        return 1

    application = create_app()

    scheduler_thread = None
    if not args.no_worker:
        scheduler_thread = scheduler.Scheduler()
        try:
            scheduler_thread.start()
        except scheduler.SchedulerAlreadyRunning as exc:
            scheduler_thread = None
            db.set_meta("scheduler.external", "1")
            print(
                f"\n  • {exc}\n"
                "    Panel açık kalacak; mesajları çalışan scheduler süreci gönderecek.\n",
                flush=True,
            )

    provider = db.get_setting("whatsapp_provider") or "simulation"
    _print_banner(
        args.host,
        args.port,
        whatsapp.provider_label(provider),
        scheduler_thread is not None,
        external=db.get_meta("scheduler.external") == "1",
    )

    if not args.no_browser:
        timer = threading.Timer(1.2, webbrowser.open, args=(f"http://{args.host}:{args.port}",))
        timer.daemon = True
        timer.start()

    logger.info("Panel başlatılıyor: http://%s:%s", args.host, args.port)
    try:
        _serve(application, args.host, args.port)
    except OSError as exc:
        print(
            f"\n  HATA: {args.port} portu kullanılamıyor ({exc}).\n"
            f"  Farklı bir port deneyin: python main.py --port 8080\n",
            flush=True,
        )
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler_thread is not None:
            scheduler_thread.stop()
            scheduler_thread.join(timeout=3)
        db.set_meta("scheduler.stopped_at", utils.to_iso(utils.now_utc()))
        logger.info("Uygulama kapatıldı.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
