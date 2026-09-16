"""Простой интерфейс диалога в терминале."""

import os
from pathlib import Path
import sqlite3

from dotenv import load_dotenv
from openai import OpenAIError

from agent import FirstAgent
from model_provider import ModelResponseError
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
from shared.agent_runtime.mcp_client import MCPConfig, MCPConnectionError, MCPToolClient


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))

    if not os.getenv("OPENAI_API_KEY"):
        print("Не найден OPENAI_API_KEY. Добавьте его в 03_first_agent/.env.")
        return

    mcp_tools = None
    try:
        mcp_config = MCPConfig.from_env()
        if mcp_config.enabled:
            mcp_tools = MCPToolClient(mcp_config)
        base_agent = FirstAgent(memory=MemoryStore(
            Path(__file__).resolve().parents[1] / "05_memory/data/memory.sqlite3", "cli"),
            allow_public_network=mcp_config.allow_public_network,
            mcp_tools=mcp_tools)
        business_store = BusinessStore(
            Path(__file__).resolve().parents[1] / "08_business_agent/data/business.sqlite3",
            "cli:local",
        )
        agent = BusinessAgent(
            base_agent, business_store,
            OpenAIBusinessExtractor(base_agent.provider.client, model=base_agent.provider.model),
            query_extractor=OpenAIBusinessQueryExtractor(
                base_agent.provider.client, model=base_agent.provider.model
            ),
            features=BusinessFeatures(
                business_store, passphrase=os.getenv("BUSINESS_BACKUP_PASSPHRASE")
            ),
            email_service=BusinessEmailService.from_env(
                base_agent.provider.client, model=base_agent.provider.model
            ),
            message_router=route_business_message,
        )
    except (ValueError, MCPConnectionError):
        if mcp_tools is not None:
            mcp_tools.close()
        print("Не удалось настроить интеграции. Проверьте MCP_* и BUSINESS_EMAIL_* в .env.")
        return
    except (OSError, sqlite3.Error):
        if mcp_tools is not None:
            mcp_tools.close()
        print("Не удалось открыть локальные данные. Проверьте доступ к каталогам data.")
        return
    try:
        _run_chat(agent)
    finally:
        if mcp_tools is not None:
            mcp_tools.close()


def _run_chat(agent) -> None:
    print(
        "Бизнес-агент готов. Напишите доход или расход обычным текстом; "
        "/summary, /money, /budgets и /templates работают локально. "
        "exit — выход."
    )

    while True:
        try:
            message = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nДо встречи!")
            break

        if message.lower() == "exit":
            print("До встречи!")
            break
        if not message:
            continue

        try:
            if message.lower() == "/reset":
                agent.reset()
                print("Диалог и бизнес-черновик сброшены. Сохранённые факты и операции остаются.")
                continue
            answer = agent.reply(message)
        except sqlite3.Error:
            print("Ошибка локальных данных. Проверьте доступ к каталогам data.")
            continue
        except ModelResponseError:
            print("Модель не завершила ответ. Попробуйте ещё раз.")
            continue
        except OpenAIError:
            print("Не удалось получить ответ от OpenAI API. Проверьте ключ, интернет и доступ к модели.")
            continue

        print(f"Агент: {answer}")


if __name__ == "__main__":
    main()
