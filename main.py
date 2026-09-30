import argparse
import asyncio

from nicegui import ui, app as guiapp

from app.config import get_config, set_config_path
from app.services.collector import hydrate_state_from_snapshot
from app.services.scheduler import start_scheduler, stop_scheduler
from app.ui.pages import build_config_ui, build_ui
from app.utils.logger import get_logger, setup_server_console_logging

log = get_logger("app", filename="app.log")


def handle_cli_args() -> None:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "-c",
        "--config",
        dest="config_path",
        help="配置文件路径，默认使用主目录下的 config.yaml",
    )
    args, _ = parser.parse_known_args()
    if args.config_path:
        set_config_path(args.config_path)


@guiapp.on_startup
async def _startup():
    # uvicorn 在启动阶段会重建部分 logger，启动后再执行一次可确保配置生效
    setup_server_console_logging(log)

    hydrate_state_from_snapshot()
    conf = get_config()
    start_scheduler(conf.check_interval_minutes)


@guiapp.on_shutdown
async def _shutdown():
    await stop_scheduler()


@ui.page('/')
async def index():
    build_ui()


@ui.page('/config')
async def config_page():
    build_config_ui()


if __name__ in {"__main__", "__mp_main__"}:
    try:
        handle_cli_args()
        setup_server_console_logging(log)

        ui.run(
            title='证书管理控制台',
            reload=False,
            show=False,
            uvicorn_logging_level="info",
        )
    except KeyboardInterrupt:
        log.info("Server stopped by user (KeyboardInterrupt)")
    except asyncio.CancelledError:
        log.info("Server stopped by asyncio.CancelledError")
    except Exception as e:
        log.error("Server crashed: %s", e, exc_info=True)
