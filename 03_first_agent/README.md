# 03 First Agent

Это терминальный интерфейс к общему `FirstAgent`, поверх которого подключён `BusinessAgent`. Он умеет собирать подтверждаемые черновики доходов и расходов; обычные вопросы, память, tools и MCP продолжают работать через прежний runtime. Подробный сценарий: [08 Business Agent](../08_business_agent/README.md).

## Проверка на своём компьютере

Нужны Python 3.11, интернет, OpenAI API-ключ и доступ к gpt-5-mini. Запросы к API могут быть платными.

Из корня проекта в PowerShell:

```powershell
if (-not (Test-Path .venv)) { py -3.11 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r 03_first_agent/requirements.txt
if (-not (Test-Path 03_first_agent/.env)) {
    Copy-Item 03_first_agent/.env.example 03_first_agent/.env
}
```

В локальном 03_first_agent/.env замените безопасный пример OPENAI_API_KEY своим ключом. Если файл уже существует, сохраните его; команда выше его не перезапишет. Корневой .env не загружается. Уже установленная переменная окружения имеет приоритет. Не отправляйте ключ в чат или Git.

```powershell
.\.venv\Scripts\python.exe 03_first_agent/main.py
```

Попробуйте: «Потратили 20 RUB на таргет сегодня для проекта Запуск», проверьте нормализованную категорию `реклама` и введите `/save`. Неполный ввод можно дополнить следующим сообщением. Отчёты, бюджеты, CSV, backup, анализ почты и подтверждаемые ответы описаны в [README бизнес-агента](../08_business_agent/README.md). Команда `exit` завершает работу.

Для обычной работы достаточно корневого `.venv`. Старое окружение `03_first_agent/.venv` подходит только после установки в него текущего `requirements.txt`, включая MCP. Подписка ChatGPT сама по себе не настраивает доступ к API.

## Как это работает

```text
Терминал → BusinessAgent → draft / отдельная SQLite
                         └→ FirstAgent → OpenAIProvider → модель/tools
```

`main.py` загружает локальный `.env` и создаёт общий runtime из `shared/agent_runtime/`. Модель видит только разрешённые схемы. `ToolRegistry` проверяет имя, права, формат и размер аргументов; `ToolRouter` направляет выбранные READ-инструменты через MCP. Результат передаётся модели с исходным `call_id`. Лимит — три вызова инструментов на одно сообщение. Финансовые вычисления выполняет код с `Decimal`.

## Основные файлы

- [main.py](main.py) — запуск терминального интерфейса и MCP-клиента.
- [../shared/agent_runtime/agent.py](../shared/agent_runtime/agent.py) — общий цикл агента.
- [../shared/agent_runtime/tools.py](../shared/agent_runtime/tools.py) — разрешения и маршрутизация инструментов.
- [../shared/agent_runtime/mcp_client.py](../shared/agent_runtime/mcp_client.py) — постоянное подключение к MCP.
- [requirements.txt](requirements.txt) — три прямые зависимости с закреплёнными версиями.

Агент работает с контрактом ModelProvider. Для подключения Claude или локальной модели позже потребуется новый адаптер, который возвращает тот же ModelReply; арифметику и логику цикла менять не нужно. Другие адаптеры сейчас не реализованы.

## Тестирование и ограничения

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Тесты проверяют бизнес-сценарий и цикл агента с поддельной моделью, а также настоящий локальный MCP-процесс без расходов на API. Последний полный прогон — 158 тестов. Живой OpenAI-диалог после Phase 8 отдельно не проверялся.

SQLite памяти хранит последние 10 обменов и до 30 фактов. Отдельная база `08_business_agent/data/business.sqlite3` хранит операции сессии `cli:local`. `/reset` очищает диалог и активный черновик, но сохраняет факты и операции. Оба каталога data исключены из Git.
