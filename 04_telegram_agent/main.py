"""Локальный запуск Telegram-интерфейса к общему FirstAgent."""

import logging
import os
from contextlib import nullcontext
from pathlib import Path
import sys

from dotenv import load_dotenv
from openai import OpenAI, DefaultHttpxClient
from httpx2 import HTTPTransport

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from shared.agent_runtime.agent import FirstAgent
from shared.agent_runtime.business import (
    BusinessAgent,
    BusinessStore,
    OpenAIBusinessExtractor,
    route_business_message,
)
from shared.agent_runtime.business_features import BusinessFeatures
from shared.agent_runtime.business_email import BusinessEmailService
from shared.agent_runtime.business_reporting import OpenAIBusinessQueryExtractor
from shared.agent_runtime.memory import MemoryStore
from shared.agent_runtime.model_provider import OpenAIProvider
from shared.agent_runtime.mcp_client import MCPConfig, MCPConnectionError, MCPToolClient
from config import Config
from telegram_adapter import TelegramAdapter
from telegram_api import TelegramAPI, TelegramError


def main():
    # Собственный logger: сетевые библиотеки не печатают URL и тела запросов.
    logging.basicConfig(level=logging.CRITICAL)
    logger = logging.getLogger("telegram_agent")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        # PowerShell 5.1 оформляет stderr native-процесса как NativeCommandError.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    runtime_logger = logging.getLogger("agent_runtime")
    runtime_logger.setLevel(logging.INFO)
    runtime_logger.propagate = False
    for handler in logger.handlers:
        if handler not in runtime_logger.handlers:
            runtime_logger.addHandler(handler)
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    try:
        config = Config.from_env()
        mcp_config = MCPConfig.from_env()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    try:
        # Ограниченное ожидание LLM; новые сообщения обрабатываются по очереди.
        # IPv6 на текущей сети недоступен. IPv4 задаём только этому клиенту,
        # без глобального monkey patch socket и без отключения проверки TLS.
        with OpenAI(timeout=60, max_retries=0, http_client=DefaultHttpxClient(
                transport=HTTPTransport(local_address="0.0.0.0"))) as client:
            mcp_context = MCPToolClient(mcp_config) if mcp_config.enabled else nullcontext(None)
            with mcp_context as mcp_tools:
                provider = OpenAIProvider(
                    client=client, reasoning_effort=config.reasoning_effort,
                    memory_enabled=True, concise_responses=True)
                base_agent = FirstAgent(provider=provider,
                    memory=MemoryStore(Path(ROOT) / "05_memory/data/memory.sqlite3",
                                       f"telegram:{config.allowed_user_id}"),
                    allow_public_network=mcp_config.allow_public_network,
                    mcp_tools=mcp_tools)
                business_store = BusinessStore(
                    Path(ROOT) / "08_business_agent/data/business.sqlite3",
                    f"telegram:{config.allowed_user_id}",
                )
                agent = BusinessAgent(
                    base_agent, business_store,
                    OpenAIBusinessExtractor(
                        client, model=provider.model, reasoning_effort=config.reasoning_effort
                    ),
                    query_extractor=OpenAIBusinessQueryExtractor(
                        client, model=provider.model, reasoning_effort=config.reasoning_effort
                    ),
                    features=BusinessFeatures(
                        business_store, passphrase=os.getenv("BUSINESS_BACKUP_PASSPHRASE")
                    ),
                    email_service=BusinessEmailService.from_env(
                        client, model=provider.model,
                        reasoning_effort=config.reasoning_effort,
                    ),
                    message_router=route_business_message,
                )
                TelegramAdapter(TelegramAPI(config.token), agent, config.allowed_user_id).run()
    except KeyboardInterrupt:
        logger.info("bot_stopped")
        return 0
    except TelegramError as error:
        logger.error("telegram_fatal code=%s", error.code)
        print("Проверьте токен (401/404), отсутствие webhook и второго экземпляра бота (409).", file=sys.stderr)
        return 1
    except MCPConnectionError:
        logger.error("mcp_connection_failed")
        print("Проверьте MCP_* в 04_telegram_agent/.env и 07_mcp/README.md.", file=sys.stderr)
        return 1
    except ValueError:
        logger.error("email_configuration_failed")
        print("Проверьте BUSINESS_EMAIL_* в 04_telegram_agent/.env.", file=sys.stderr)
        return 1
    except Exception:
        logger.error("startup_or_runtime_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
