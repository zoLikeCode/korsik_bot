# Работа с фактическим API

Контракт из исполняемого FastAPI: [openapi.json](openapi.json), [openapi.yaml](openapi.yaml). Локальный серверный каталог без JS/CDN — `/api/docs`, JSON на сервере — `/api/openapi.json`. Бизнес-маршруты имеют префикс `/api/v1`; внутренний transport API требует отдельного служебного ключа и не является браузерным API.

Схема описывает реальные маршруты. Типизированные команды, включая диалоговый `TurnInput`, запрещают дополнительные поля. Часть административных команд принимает JSON-объект и проверяет его в обработчике; для них OpenAPI не заменяет проверки в коде и приведённые ниже последовательности. Проектный OpenAPI из соседнего `korsik-spec` сохранён как исторический дизайн.

## Сессия и повторные запросы

`POST /auth/login` принимает `email` и `password`. Ответ устанавливает `korsik_session` (HttpOnly) и `korsik_csrf`, возвращает `csrf_token` и текущего пользователя. Браузер отправляет cookies; изменение данных требует заголовков:

```http
X-CSRF-Token: <csrf_token из ответа входа>
Idempotency-Key: <UUID конкретной команды>
```

При сетевом повторе той же команды сохраните ключ и тело. Другой пользователь, сменившаяся область доступа или изменённое тело не наследуют прежний успешный ответ. Для редактируемого объекта получите свежий `ETag` и передайте его как `If-Match`. Не создавайте ETag из номера версии самостоятельно: используйте token, выданный сервером. HTTPS-прокси может добавить `W/` при сжатии JSON. Наш API-клиент удаляет только этот префикс у application entity-version token, сохраняя остальное значение; backend продолжает строго сравнивать текущую версию. Это правило нашего CAS-контракта, а не универсальное разрешение применять weak HTTP ETag к произвольному If-Match.

Основные отказы: `401` — нужна сессия; `403/404` — нет доступа; `409` — конфликт состояния/проверки; `412` — объект изменён; `428` — отсутствует обязательный `If-Match`; `422` — некорректные данные; `507` — недостаточно места. `detail` может содержать строку либо структурированный объект; интерфейс учитывает оба варианта.

## Свободный вопрос сотрудника

1. `POST /conversations` с `employee_id` открывает текущий диалог доступного сотрудника.
2. `POST /conversations/{id}/turns` отправляет новую реплику:

```json
{
  "client_message_id": "c80d0dc5-a70a-4e24-b135-97e3935a42d7",
  "input": {
    "kind": "TEXT",
    "text": "Как передавать пароли к конфиденциальной информации?"
  }
}
```

Обычный вопрос создаёт durable `ANSWER`; ответ содержит `dispatch=BOT_ANSWER` и `answer_id`. Опросите `GET /answers/{id}` до терминального состояния, затем обновите сообщения диалога. Различайте `LOCAL_GENERATED`, `EXTRACT`, `CLARIFY`, `HANDOFF` и фактический `reason_code`; наличие текста не доказывает локальную генерацию. В режиме HUMAN сообщение получает HR и автоматическая консультация не запускается.

Кнопка вопроса отправляет тот же turn с `input.kind=RAG_QUERY`, `menu_id`, `menu_version_id`, `prompt_key`. Текст берётся из опубликованного меню на сервере. Клиент не передаёт скрытый готовый ответ или system prompt.

Ответ на анкету использует отдельные typed survey/capture-команды, а не обычную реплику. Для свободного текста анкеты предусмотрен черновик с явным подтверждением. Это сохраняет возможность задать ассистенту вопрос посреди опроса.

## MAX и личность

HR создаёт карточку через `POST /employees`, затем `POST /employees/{id}/binding-invitations`. Одноразовая ссылка возвращается только в ответе создания, в БД остаётся hash. После перехода из MAX привязка имеет `PENDING_VERIFICATION`; HR сверяет человека и подтверждает `POST /channel-bindings/{id}/approve` с текущим ETag. Произвольный MAX ID не активирует доступ.

Руководитель/HR подключает собственные уведомления через `/me/notification-bindings`: challenge в MAX и отдельное подтверждение в той же веб-сессии. Это не создаёт фиктивного сотрудника или onboarding episode.

Бот задаётся `MAX_BOT_URL`, секрет — отдельным файлом `max_token`. Меняя бота при переносе, измените оба параметра и настройте webhook/корпоративный HTTPS. Служебные endpoints connector недоступны по employee session.

## Публикация документа

Рабочая последовательность соответствует интерфейсу HR:

1. `POST /knowledge/documents`: название, категория и структурированная область применения.
2. `POST /knowledge/documents/{id}/revisions`: multipart file, причина и при необходимости сроки действия; свежий ETag документа. Полученную операцию отслеживайте через `GET /operations/{id}`.
3. Проверьте `/knowledge/revisions/{id}/pages` и `/extraction`. Исправления блока создают новую extraction revision; значимые изображения требуют описания либо обоснованного исключения. Для page review используйте текущий extraction ID и ETag.
4. `POST /knowledge/revisions/{id}/candidate-build` ставит построение кандидата в очередь. Сначала дождитесь `SUCCEEDED`; исходный HTTP `202` не означает готовый индекс.
5. `POST /knowledge/revisions/{id}/qa-runs` создаёт отдельную durable проверку. Контрольные вопросы, snapshot, модель и извлечение зафиксированы; изменение любого из них требует повторной проверки.
6. Заполните ручную оценку ответов и согласуйте через `/review`. Непройденную автоматическую проверку нельзя отменить простой отметкой HR.
7. `/publish` атомарно переключает действующий снимок. Загрузка, OCR, кандидат и одобрение сами по себе не публикуют источник.

Для совместной публикации файла и зависимой инструкции передайте `additional_revisions` с точными revision/extraction/content pins в candidate-build. Каждому участнику нужна собственная оценка и одобрение; публикация переключает весь кандидат одной транзакцией. Текущая база остаётся доступной при ошибке построения.

`GET /knowledge/documents/{id}/warnings` даёт доступные текущему HR предупреждения о совпадении файлов, похожих названиях и потенциальном пересечении применимости. Это подсказки для сравнения, а не автоматическое заключение о противоречии документов.

Отмена поддерживается только для безопасно отменяемых операций; наблюдайте терминальное состояние. PURGE/PUBLISH/ROLLBACK не прерываются произвольным cancel. Откат создаёт новую проверяемую редакцию; он не переписывает опубликованную историю.

## Сценарии и отчёты

Черновик программы: `/scenarios/{id}/versions` → сохранение графа → `/validate` → `/simulations` → `/review` → `/publish`. Опубликованная версия неизменяема. Если изменены обязательные исходные шаги, review требует `source_change_acknowledged=true`, актуальный `source_change_fingerprint` и `source_change_reason`. Смена опубликованной базовой версии инвалидирует прежнее согласование. Layout графа не определяет выполнение.

Список программ содержит `scope` и `can_edit`. Региональный HR может читать применимые общие программы и редактировать свои; публикация общей программы требует соответствующих полномочий. Миграция действующего участника использует отдельные plan preview/hash/apply и не меняет завершённые действия или активные ответы анкеты.

Для аналитики передавайте даты и IANA timezone, используйте возвращённый контекст окна и `metric_details`. Текущий backlog/статус сотрудника помечен как состояние на момент расчёта, а не реконструкция прошлого. Выгрузки асинхронны; роль и область проверяются заново при скачивании готового файла. Малые группы не раскрывают raw HR-ответы руководителю.

Эксплуатационные команды, модельные профили, резервирование и реальные измерения описаны в [OPERATIONS.md](OPERATIONS.md), [ARCHITECTURE.md](ARCHITECTURE.md) и `tests/*report.json`.


## MAX MiniApp: серверная сессия

`POST /auth/max-miniapp` принимает только `{"init_data": "<исходная подписанная строка MAX>"}`. Endpoint проверяет подпись, время и replay, затем разрешает роль только через текущую подтверждённую server binding и User. `role`, `scope`, `employee_id` во входном теле не принимаются. Неизвестный MAX user не создаёт карточку/повышенный аккаунт. Для уже подтверждённого сотрудника без User provision ограничен EMPLOYEE его существующей карточки. Cookies Secure/SameSite=None и CSRF выдаются после проверки; обычный web login остаётся. Факт локального synthetic initData теста не подтверждает настройку MiniApp владельцем бота или реальный launch на устройстве.

Основные коды: `max_init_data_invalid`, `max_init_data_expired`, `max_init_data_future`, `max_init_data_replayed`, `max_account_not_linked`, `max_identity_ambiguous`, `max_account_unavailable`, `max_https_required`. Используйте HTTP status и `detail.code/message`; не исправляйте отказ назначением роли на клиенте.

## Дополнительные опросы и расписание

Фактическая схема содержит 211 paths и 252 operations. Новые families:

| Маршруты после `/api/v1` | Последовательность и границы |
| --- | --- |
| `/survey-templates`, `/survey-templates/{id}` | Scoped HR создаёт шаблон/первый draft и читает версии. |
| `/survey-templates/{id}/versions` | Новая версия вместо изменения опубликованной; причина + ETag шаблона. |
| `/survey-template-versions/{id}` | Чтение разрешённой версии; изменение только DRAFT с ETag. Typed questions LIKERT/SINGLE_CHOICE/MULTI_CHOICE/TEXT. |
| `/survey-template-versions/{id}/preview`, `/submit-review`, `/review`, `/publish` | Предпросмотр сохранённой версии, проверка, решение APPROVE/REJECT, публикация HR_ADMIN. |
| `/survey-campaigns/preview`, `/survey-campaigns` | Проверка фактических recipients/episodes, затем сохранение с `preview_hash`. |
| `/survey-campaigns/{id}` PATCH, `/cancel` POST | CAS/причина; replan только до открытого run, cancel не удаляет ответы. |
| `/survey-campaigns/{id}/responses`, `/analytics` | Raw scoped HR отдельно от агрегатов MANAGER/HEAD с n≥5 и подавлением редких клеток. |
| `/me/custom-surveys` | Собственные runs текущего episode; ответы через существующие `/survey-runs/{id}/answers`/`commands`/capture. |

Даты Schedule обязаны содержать UTC offset; timezone — IANA. UI разбирает wall time и неоднозначность DST до отправки. Reminder hours относятся к фактическому opening receipt. Это single-period campaign, не новый произвольный cron interpreter. Политика участия — VOLUNTARY_IDENTIFIED: ответы связаны с карточкой, все вопросы допускают пропуск, отдельного anonymous/diagnostic режима нет.

`HANDOFF` от поиска теперь создаёт pending support case с нейтральной репликой без остановки BOT. Claim через `/cases/{id}/claim` явно включает HUMAN; `/conversations/{id}/messages` отправляет подписанный как ответ команды поддержки текст HR. Старый ручной `/handoff` не равен новому неявному ожиданию. Расположение пунктов UI не меняет endpoint RBAC.

## Обновление экспортов OpenAPI

JSON/YAML генерируются из актуального `korsik.main.app.openapi()` в локальном процессе без подключения к БД/MAX/моделям. Ручная проектная схема рядом в `korsik-spec` не подменяет этот экспорт. Пример воспроизводимого экспорта из каталога приложения:

```sh
KORSIK_TESTING=true KORSIK_DATABASE_URL='sqlite:///:memory:' PYTHONPATH=backend .venv/bin/python - <<'PY'
import json
from pathlib import Path
import yaml
from korsik.main import app
schema = app.openapi()
Path('openapi.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
Path('openapi.yaml').write_text(yaml.safe_dump(schema, allow_unicode=True, sort_keys=False, width=120))
PY
```

PyYAML используется локальным tooling, не добавляет новый runtime dependency. Части response schemas остаются свободными JSON-объектами, а cookie/CSRF/internal key checks реализованы middleware/dependencies: отсутствие `securitySchemes` у автосхемы **не** означает публичный доступ. Проверка `/api/openapi.json` именно нового сервера после deployment остаётся отдельной проверкой; локальная equality/refs проверка записана в `qa/openapi-integration-export.json`.
