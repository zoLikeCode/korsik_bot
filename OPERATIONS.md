# Корсик: запуск, обновление и восстановление

Инструкция для фактического Docker Compose приложения, 24.10.2026. Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md). Адрес текущего демонстрационного входа и учётные данные хранятся отдельно; этот файл не содержит секретов. Команды выполняет оператор из каталога приложения, где находятся `compose.yaml` и закрытый `.env`.

Команды ниже описывают запуск, обновление и восстановление. HTTP-контракт доступен в `/api/docs` и [API-GUIDE.md](API-GUIDE.md).

## 1. Подготовка инфраструктуры

Linux amd64, Docker Engine и Compose >=2.24.4. Для FULL нужны NVIDIA driver/Container Toolkit и совместимая GPU; текущий generator default 4B рассчитан на локальную CUDA. Конкретный расход памяти/скорость подтверждаются runtime report данного оборудования. Для полного набора сервисов ориентир 32–48 ГБ RAM; это sizing ориентир, не измеренный максимум любой нагрузки. CORE запускается без decoder/GPU. CPU encoder/reranker реально проверялись в ONNX FP32 на AVX без AVX2.

| Порт/маршрут | Доступность по базовому Compose |
| --- | --- |
| `127.0.0.1:8080` | Единственный host-published кабинет. Меняется через `BIND_IP`/`WEB_PORT`. Для внешнего рабочего входа нужен доверенный HTTPS. |
| API `:8000` | Только внутренние сети Compose, `/api/v1`, `/api/docs`, `/healthz`, `/readyz`. |
| Connector `:8001` | Только внутренние сети; Nginx направляет `/hooks/max` и `/api/v1/integrations/max/webhook`. |
| PostgreSQL `:5432`, Qdrant `:6333`, models/parser `:8000` | Не публикуются на хост. |

Не публикуйте PostgreSQL/Qdrant/models ради удалённого администрирования. Разрешённый MAX egress принадлежит connector; внутренним AI-сервисам интернет не требуется.

### Подключённая сборка

```sh
cp .env.example .env
python3 scripts/init-secrets.py --directory /private/korsik-secrets --owner 10001
python3 scripts/fetch-models.py --destination /opt/korsik/models
python3 scripts/install-quality-report.py --bundle /opt/korsik/models/generator-4b
python3 services/model_server/fetch_parser_models.py --target /opt/korsik/models/docling
docker build -t korsik-parser:local -f services/model_server/Dockerfile.parser .
docker compose build
```

`fetch-models.py` загружает веса и manifest, но не создаёт оценку качества. Отдельная явная команда `install-quality-report.py` переносит поставляемый ограниченный инженерный review только при точном совпадении SHA256 manifest, каждого файла весов, текущего retrieval pipeline и кода generation runtime. Отказ означает необходимость новой смысловой проверки, а не повод вручную выставить PASS. Сохранённые неудачные raw-model прогоны и ограничения входят в отчёт. При запуске FULL профиль дополнительно проходит реальную runtime validation; этот review не является приёмкой HR компании. Готовый offline bundle уже содержит отчёт выбранного артефакта.

Укажите в `.env` абсолютные `SECRET_DIR`, `MODEL_DIR`, `PUBLIC_URL`, `MAX_PUBLIC_URL`, режим и доступный `GENERATOR_GPU_ID`. `PUBLIC_URL` должен совпадать с реальным origin: он задаёт абсолютные ссылки и HTTPS-конфигурацию. CSRF/Origin проверяется по фактическому Origin/Host запроса, а не по доверию к сохранённой строке PUBLIC_URL. На demo Quick Tunnel при автоматическом discovery оставьте `MAX_PUBLIC_URL` неустановленным; фиксированный устаревший URL отключает смысл автообнаружения. Для on-prem задаётся стабильный согласованный origin. Для штатного HTTPS используйте `SECURE_COOKIES=true`.

`init-secrets.py` создаёт отдельные SQL/service/session/bootstrap секреты. Реальный MAX token устанавливается оператором отдельно в `SECRET_DIR/max_token`, не в исходники, Dockerfile, `.env` или командную строку. App UID10001 должен читать свои secrets/model files. Для PostgreSQL secret `db_bootstrap` требуется доступ пользователю его образа; закрытый родительский каталог сохраняет ограничение доступа. Созданный bootstrap admin пароль не переписывает существующую учётную запись при повторном migrate.

### Изолированная поставка

На сборочном стенде:

```sh
MODEL_DIR=/opt/korsik/models scripts/export-offline-bundle.sh /secure/korsik-offline-bundle
```

Результат: `images.tar.gz`, `models.tar.gz`, `source.tar.gz`, `manifest.json`, `INSTALL.txt`. Веса, pinned revisions, manifests и лицензии входят. Source archive содержит пример исходного кейса и инженерные QA-отчёты с примерами источников/синтетических вопросов; рабочие PostgreSQL/blobs и секреты в него не входят. Каталог поставки должен быть новым, ещё не существующим, вне исходного checkout и каталога моделей. Сначала проверьте SHA256 всех трёх архивов по manifest, затем:

```sh
docker load < images.tar.gz
mkdir -p app models
tar -xzf source.tar.gz -C app
tar -xzf models.tar.gz -C models
chown -R 10001:10001 models
cd app
cp .env.example .env
```

Настройте локальные пути/origin и отдельно provision secrets. Для on-prem:

```sh
docker compose -f compose.yaml -f deploy/compose.onprem.yaml up -d --no-build --pull never
```

Корпоративный reverse proxy завершает TLS443 и направляет кабинет на loopback WEB_PORT. On-prem override отключает demo tunnel и требует `MAX_PUBLIC_URL`. Интернет не используется для сборки/загрузки моделей во время этого старта. MAX должен быть отдельно разрешён сетью компании.

Для явного CORE добавьте override ко **всем** командам управления этим deployment:

```sh
docker compose -f compose.yaml -f deploy/compose.onprem.yaml -f deploy/compose.core.yaml up -d --no-build --pull never
```

Не называйте CORE полной приёмкой свободного LLM диалога. Этот режим сохраняет сценарии/анкеты/HR и показывает ограничения ответа.

## 2. Первый запуск и проверка

`init-data` устанавливает владельца data volumes. `migrate` создаёт схему, отдельные SQL-роли, начальную HR_ADMIN учётную запись и idempotent seed. Затем запускаются API/workers/scheduler/models/web/transport.

```sh
docker compose ps
docker compose logs --tail 50 migrate api worker answer-worker ingest-worker scheduler
docker compose exec -T api python -m korsik.manage doctor
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').status)"
```

`readyz` подтверждает БД, а не качество модели. Проверьте технический статус и local AI profiles через защищённые `/api/v1/system/status`, `/api/v1/local-ai/profiles`, `/api/v1/local-ai/active-profile` либо операторский CLI: проверьте READY сервисы, selected artifacts/fingerprints, active profile, опубликованный KB snapshot и FULL/CORE/degraded status. Для нового профиля выполните реальные tests, дождитесь validation, затем Activate. Активация при незавершённых ответах требует drain; наличие файлов модели ещё не означает активацию.

При выявленном смысловом FAIL профиль нельзя возвращать в работу только по READY или старому runtime validation. Сохраните точные вопросы, разрешённые источники и ошибочные ответы в закрытом отчёте инцидента, приостановите генерацию через уполномоченного оператора и оставьте безопасный режим ответов. После исправления нужна новая смысловая проверка на фактических источниках, привязанная к текущим весам, retrieval policy и generation runtime; затем новый совместимый профиль, его реальный validation и явная активация. Перезапуск контейнера сам по себе не является исправлением качества. Исторические PASS и FAIL отчёты сохраняются раздельно.

На demo стенде временный HTTPS origin можно узнать без секретов:

```sh
docker compose exec -T tunnel cat /run/tunnel/public_url
```

После пересоздания tunnel адрес может измениться. Обновите `PUBLIC_URL`, зарегистрированный MAX origin и точку входа пользователей; это ограничение временного стенда. Корпоративный стабильный HTTPS устраняет смену origin. Не публикуйте в отчётах полные cookies/tokens и содержимое secret mounts.

## 3. Подготовка HR через интерфейс

1. **Справочник.** Проверьте юрлица/площадки, подразделения/назначения HR/руководителей, timezone и тип сотрудника. Для массового внесения используйте CSV preview, исправьте ошибки, затем apply. Новая площадка создаётся формой, без изменения кода.
2. **Календарь.** Создайте weekly intervals/dated exceptions, проверьте preview влияния и активируйте. Seed-пятидневка демонстрационная. Нет подтверждённого покрытия — нет обещания правильной сменной даты/SLA.
3. **Материалы и программа.** Загрузите реальные недостающие файлы/изображения/карты/персональные материалы, проверьте scope. В draft конструктора исправьте source warnings, прогоните simulation/review diff. Publish новой версии и migration действующих сотрудников — разные операции.
4. **Меню.** Добавьте labels/query_text и порядок быстрых вопросов, согласуйте и опубликуйте отдельную menu version. Кнопка запускает тот же поиск, что свободный текст.
5. **Знания.** Upload → processing → страницы/исправления OCR → candidate → контрольные вопросы/QA → ручной review/impact → publish. В metadata укажите ответственного; для смены HR_ADMIN выбирает включённого HR с правом на scope документа и указывает причину. При смене scope одновременно замените ответственного, если прежний теряет доступ. Сравнение с прежней редакцией показывает структурный diff и явные зависимости. Обработанный upload сам по себе не является опубликованным знанием. Описание смысловой картинки проверяет человек.
6. **Сотрудник.** Создайте карточку/программу/даты/назначения, выдайте одноразовое приглашение MAX. После redemption HR подтверждает личность. Стадия PREBOARDING/ACTIVE подтверждается сейчас либо отдельной запланированной командой с явным часовым поясом. Pending/history показывает due и результат; отмена требует причины и текущей версии операции. Новая стадия отменяет прежнее pending назначение; OFFBOARD применяется немедленно. Чужой аккаунт не получает доступ только по введённому имени.
7. **Коммуникации.** Для dashboard/digest notifications руководитель/HR подтверждает собственную MAX-привязку и включает preference. Legacy verified recruiter contact сохраняется в служебной конфигурации из реально предоставленной ссылки; при отсутствии ссылки работает внутренний handoff.
8. **Работа.** Claim case → ответ в HUMAN → явный resume BOT. Для BLOCKER укажите исправление, затем дождитесь employee confirmation. BIO/PHOTO рассылаются только после preview/approval конкретных recipients.

Согласуйте taxonomy отзывов и golden вопросы. Synthetic sentiment baseline всегда требует HR review; он не является обученной на компании оценкой риска.

## 4. Обновление приложения и модели

До изменения сохраните backup, manifest/images IDs текущего release и закрытый `.env`. Перенесите заранее собранные проверенные образы/исходники. Не заменяйте рабочие данные seed-каталогом и не выполняйте `down -v` на рабочем проекте.

```sh
scripts/backup.sh /secure/korsik-backups
docker compose images
docker compose build api web
docker compose up -d --no-build
docker compose ps
```

В изолированном контуре вместо `build` загрузите образы из утверждённой поставки и добавьте `--pull never`. Используйте те же override files, что при запуске. После обновления повторите readiness, native HR smoke и проверку MAX без массовой тестовой рассылки.

Для смены decoder подготовьте новый pinned каталог заранее, задайте `GENERATOR_MODEL_SUBDIR`, перезапустите generator и соответствующую конфигурацию app. Создайте/проверьте профиль с фактическими installed artifacts, выполните validation и активируйте после drain. Старый профиль не считается валидным для иных runtime fingerprints. Операторский restart обязателен; hot reload weights не реализован. При смене encoder требуется проверенный rebuild vector projection и candidate regression.

Изменение chunking/retrieval/prompt кода также требует нового профиля: конфигурация содержит `pipeline_identities`, в том числе SHA256 полного retrieval module (константы/helpers) и диагностические AST hashes; runtime проверяет фактический SHA256 renderer. Старый отчёт PASS с несовпадающим кодом получает `STALE_PIPELINE_REVIEW` и не разрешает новую активацию. Activate заново читает текущий реестр/quality report, проверяет выбранные installed artifacts, включённую генерацию и фактические health fingerprints весов/renderer; прежний PASSED validation не обходит последующий FAIL, смену pipeline или deployment. Не копируйте старую отметку PASS в новую поставку без повторной оценки. Старые chunks без записанной версии явно имеют `LEGACY_UNVERSIONED`; это не утверждение о восстановимости их прежнего runtime.

**Rollback кода:** используйте ранее сохранённые image IDs/tags и совместимый source release. Сначала проверьте schema compatibility. Если миграция несовместима, нужен согласованный restore пары SQL+blobs, а не только старый контейнер. Автоматического безопасного downgrade любой будущей схемы нет.

**Rollback документа:** в нативном KB выберите прежнюю редакцию, укажите причину. Workflow создаёт новую редакцию и выполняет нужные impact/QA/review gates. Не редактируйте `knowledge_head` вручную и не возвращайте архивный Qdrant index вместо SQL-состояния.

## 5. Очереди и сбои

| Наблюдение | Действие |
| --- | --- |
| `PENDING`/`RETRY` Job | Проверьте worker соответствующего класса, due time и health. Scheduler автоматически возвращает истёкший PROCESSING lease в retry, с лимитом attempts. |
| `FAILED` Job | Устраните причину; global HR/PLATFORM может выполнить retry в настройках. Активный PROCESSING job нельзя принудительно перезаписать через retry. Terminal answer не должен порождать второе сообщение от повторного job. |
| `WAITING_BINDING` Outbox | HR проверяет действующую MAX-привязку; обходить verification редактированием SQL нельзя. |
| `SUPPRESSED` | Смотрите причину: HUMAN, ENDED/новый episode, отозванный источник, scope, изменённая menu/question revision или schedule. Это защитный итог, а не приглашение слепо переотправить запись. |
| `UNKNOWN_DELIVERY` / delivery `UNKNOWN` | MAX мог принять сообщение до потери сети/lease. Автоповтор отключён. Сопоставьте platform message ID/реальную историю с уполномоченным участником; не меняйте статус на RETRY только для очистки счётчика. Если нужна новая ручная коммуникация, HR делает её отдельным осознанным действием после сверки. Generic retry endpoint не является reconciliation внешней доставки. |
| Generator недоступен | FULL явно деградирует к разрешённой выдержке/уточнению/HR. Анкеты/captures/handoff сохраняются независимо; восстановите service/profile. Для длительной работы без decoder включите явный CORE override. |
| Смысловой FAIL / генерация намеренно приостановлена | Сервис может оставаться READY. Не переактивируйте старый профиль по прежнему smoke test. Зафиксируйте инцидент, исправьте причину, выполните свежую смысловую проверку текущего pipeline и затем validation/активацию нового совместимого профиля. До этого сохраняется явная безопасная деградация. |
| `GENERATION_BUDGET_EXCEEDED` / `INCOMPLETE_GENERATION` | Незавершённый текст модели не отправляется. Используется полный ограниченный набор выдержек либо HANDOFF, если безопасная выдержка слишком велика. Проверьте queue/inference/request timings и загрузку GPU. Текущий inference budget17с и динамический request budget≤20с ограничивают ожидание; они не доказывают p95 ответа без отдельного измерения. Изменение prompt/runtime требует новой привязанной проверки качества. |
| `SECTION_CONTEXT_TOO_COMPLEX` | Связанные условия раздела требуют более шести страниц; ответ передан HR без усечения обязательных правил. Уточните предмет вопроса или подготовьте проверенную инструкцию с полными зависимостями. Увеличение лимита без проверки полноты не устраняет причину. |
| OCR/parse ошибка | HR открывает failed revision и page warnings, исправляет/повторяет выбранные страницы. Не публикуйте непроверенные тексты/таблицы ради зелёного статуса. |
| Ingest/QA надо отменить | Нативная отмена операции; допустимы только supported cancel types. Purge/rollback не отменяются общим cancel без компенсации. |
| Disk full / upload507 | Освободите место по утверждённой политике, повторите revision upload/retry. Действующий KB snapshot сохраняется; не удаляйте произвольные blobs опубликованных редакций. |
| Qdrant потерян | Восстановите сервис и выполните rebuild из опубликованного SQL/immutable extraction; команда ниже. |

```sh
docker compose logs --tail 100 answer-worker ingest-worker worker scheduler max-connector
docker compose run --rm --no-deps api python -m korsik.manage reindex
```

Не выгружайте полный raw SQL/Job payload/переписку в технический ticket. Технический jobs API отдаёт metadata. Свободные HR audit reasons хранятся приватно и доступны только global HR; PLATFORM не получает их вместе с техническим журналом.

## 6. Резервирование и retention

```sh
scripts/backup.sh /secure/korsik-backups
sudo scripts/install-backup-timer.sh /opt/korsik/app /secure/korsik-backups
systemctl list-timers korsik-backup.timer --no-pager
journalctl -u korsik-backup.service --since today --no-pager
```

Host systemd timer: 00:15/06:15/12:15/18:15 UTC, randomized delay до2 минут, Persistent. Это расписание не доказывает RPO без успешных копий/мониторинга. Timer установлен оператором на хосте, отдельного cron/Airflow контейнера нет.

Backup содержит PostgreSQL custom dump, immutable blobs archive, image metadata и SHA256 manifest. Скрипт держит purge-exclusion advisory lock `76810108` на период SQL snapshot → blobs tar: файлы не удаляются между этими этапами. Новые append-only файлы допустимы; неподтверждённые лишние файлы в архиве не становятся опубликованным знанием. Qdrant восстанавливается из SQL. Secrets и данные MAX outside-platform не входят в этот backup.

Source15 репетиция использовала backup `20261024T025441Z`; private off-VM copy проверена SHA3/3, host timer после FULL/CORE benchmark восстановлен ACTIVE. Точное состояние и время следующего запуска находятся в `tests/backup-offhost-report.json`/`tests/backup-timer-status.json`; этот исторический отчёт не заменяет текущий мониторинг.

**Off-VM copy и retention исполняет оператор.** Скопируйте проверенный архив на отдельный защищённый носитель/хранилище, проверьте checksum там, ограничьте доступ и зафиксируйте срок/успех. Копия только на той же VM не защищает от потери VM. Backup age, свободное место, результат timer и успешный restore должны мониториться. Автоматического удаления host backups по UI policy нет.

Knowledge purge по умолчанию закрыт, пока global HR не установит реальную утверждённую company policy: backup retention, approval reference/confirmation, отсутствие hold. Purge немедленно отзывает source, затем удаляет допустимые online artifacts по workflow; он не стирает уже выданные MAX сообщения или чужие/off-VM backups. Даты истечения копий и фактическое удаление резервов остаются ответственностью оператора. Кадровые данные не удаляются автоматически по произвольному сроку. Технический sweep удаляет истёкшие sessions, DONE transport inbox старше30 дней и завершённые idempotency records старше7 дней.

## 7. Репетиция восстановления и доказательства

Быстрая проверка dump/blobs:

```sh
scripts/check-restore.sh /secure/korsik-backups/TIMESTAMP
```

Она создаёт временную БД, проверяет dump/schema и архив, затем удаляет тестовую БД. Это не полноценный runtime restore.

Полный отдельный Compose cold restore, со своими secrets/volumes и без MAX/tunnel/web:

```sh
OFFLINE_CHECK_GPU_ID=0 OFFLINE_CHECK_GENERATOR_SUBDIR=generator-4b \
  scripts/check-offline-restore.sh /secure/korsik-backups/TIMESTAMP /secure/korsik-check-full
```

GPU должна быть свободной и отличаться от production GPU. Не останавливайте рабочий generator для этой проверки. FULL probe проверяет cold services, synthetic Russian image-only PDF OCR, Qdrant rebuild, локальный ответ/feedback/dashboard; затем выключает **только isolated generator** и проверяет реальные business commits параллельно с degraded RAG. ENOSPC возникает в отдельном tmpfs1MiB. Отчёты сохраняются до cleanup; trap удаляет отдельный project/volumes. Failed check сохраняет failure-state/services logs.

Для отдельной проверки controlled CUDA allocator OOM есть opt-in hook `tests/isolated_cuda_oom/run-cuda-oom-hook.sh` с инструкцией в соседнем README. Он применим только к отдельному restored project на свободном GPU0 при production на другом GPU, после замеров производительности. Hook ограничивает allocator после загрузки неизменённой 4B модели, фиксирует настоящее исключение и бизнес-коммиты, затем снимает cap и проверяет реальный generate recovery. Это не физическое исчерпание всей VRAM. Source15 actual:6OOM/14checksPASS/business1.926с/recovery200; исходные JSON сохранены. В fault-наборе только один из трёх RAG-вопросов требует decoder; остальные два выбирают нормативную выдержку.

CORE и реальный HTTP latency workload:

```sh
OFFLINE_CHECK_MODE=CORE OFFLINE_CHECK_CORE_LATENCY=true \
  OFFLINE_CHECK_ADMIN_PASSWORD_FILE=/private/korsik-secrets/admin_password \
  scripts/check-offline-restore.sh /secure/korsik-backups/TIMESTAMP /secure/korsik-check-core
```

Пароль монтируется только в benchmark helper, не печатается. 100 distinct prompts проходят HTTP session/CSRF/idempotency → PostgreSQL ANSWER job → answer-worker → persisted result. Helper использует явно синтетическую PERF карточку, завершает её/отзывает доступ после замера; external MAX не стартует. Нельзя называть timing workload корпоративным golden set. Фактические p50/p95/queue/stage поля берите из JSON данного image ID, не переносите значения с другой версии/VM.

Для source15 измерены: FULL100/100COMPLETED, overallHTTPp95 26.0687с, generated-only n10/p95 26.2059с; отдельный CORE100/100EXTRACT,p95 1.0161с. Оба actual контекста закреплены за backend image `38206fd7…a72c5d8`,0MAX и cleanup0. Полный/CORE отчёты — `tests/api-latency-full.json`, `tests/api-latency-core-restored.json`; исторические source11/CORE8 результаты сохранены отдельно. Frontend WEB18 после этих замеров имеет только дополнительную проверку личности при HR binding approval; backend/answer path не менялся.

PostgreSQL concurrency/fault suite:

```sh
scripts/check-postgres-integration.sh /secure/korsik-pg-check
```

Скрипт копирует **только схему** в строго `korsik_integration_check`, запускает synthetic fixtures в текущем deployed image, сохраняет результат и удаляет БД. Существующую тестовую БД не перезаписывает. Проверяются duplicate MAX/turn, message sequence, capture isolation, stale callback, double scheduler, lease fencing, реальный SIGKILL worker/reclaim, UNKNOWN no-retry, cancel/checkpoint/retry races, атомарная concurrent multi-member publication и actual scheduled-publish worker: успех, отзыв прав автора и rollback после ошибки уже выполненной записи. Внешних MAX/model sends в этом suite нет; модельное качество он не доказывает.

## 8. Восстановление рабочего контура после аварии

Сначала выполните репетицию на отдельном проекте и выберите совместимые backup/source/images/model manifests. Предпочтительнее восстановить на новых volumes, сохранив повреждённый экземпляр для разбора. Следующие действия — отдельная операторская процедура с согласованным downtime, не команда для проверки «на живой» БД.

1. Зафиксируйте backup cutoff и прекращение внешней отправки. Остановите старый connector/compute. Не удаляйте UNKNOWN/ACCEPTED историю.
2. Provision совместимые images/models и secrets на целевой установке. Поднимите только init-data/PostgreSQL; новая БД должна быть пустой.
3. Восстановите SQL и blobs. Пример с отдельным project name:

```sh
docker compose -p korsik_recovered up -d init-data postgres
docker compose -p korsik_recovered exec -T postgres pg_restore \
  -U korsik_bootstrap -d korsik --no-owner --no-acl --exit-on-error < /secure/backup/database.dump
docker compose -p korsik_recovered run --rm --no-deps -T --user 10001:10001 \
  migrate tar --no-same-owner -C /data -xzf - < /secure/backup/blobs.tar.gz
docker compose -p korsik_recovered run --rm migrate
```

Дождитесь готовности PostgreSQL перед pg_restore; ошибку restore нельзя игнорировать. Используйте сохранённые override files целевой установки. Повторный bootstrap не сбрасывает существующие пароли; оригинальные credentials/recovery process должны храниться отдельно от data backup.

4. Поднимите data/model services и API/workers/scheduler без MAX connector. Выполните `manage reindex`, readiness, source ACL, native HR/анкеты и профили моделей. Не переключайте публичный reverse proxy до проверки.
5. Сверьте outbox и MAX историю после cutoff: backup может не содержать receipt уже доставленного сообщения. Историю внешней платформы нельзя восстановить транзакцией PostgreSQL. Приостановленная/неопределённая доставка требует человеческой сверки.
6. После утверждения включите connector/web и стабильный HTTPS origin, проверьте webhook/binding на разрешённом тестовом участнике. Запишите фактический RTO/RPO и дату следующего restore rehearsal.

Включение старого outbound без сверки не является безопасным disaster recovery. Изолированные proof scripts специально не включают MAX и не изменяют production.


## Дополнение: текущая оболочка и ограниченное host recovery

Опросы создаются в отдельном экране «Опросы»: draft вопросов → сохранение → preview → review → HR_ADMIN publish → audience/timezone preview → закреплённое расписание. Это identified voluntary опросы, не анонимная психологическая диагностика. Ответы не входят в базовую завершённость адаптации и общую sentiment analysis. HTTP-маршруты описаны в [API-GUIDE.md](API-GUIDE.md).

Перед плановой остановкой сервиса установите `/opt/korsik/runtime/maintenance`; для восстановления распознаётся также `restore-in-progress`. Boot/watchdog units не включаются наличием исходного файла: требуется явный installer и проверка systemd оператором. Базовый whitelist содержит 14 FULL services; исключение generator/tunnel в CORE/on-prem должно быть отражено в boot arguments и whitelist, иначе отсутствие будет воспринято как отказ. Heartbeat/health/policy tests не доказывают успешную перезагрузку хоста. Команды установки, ограничения restart и checklist фактического выполнения приведены в том же дополнении.
