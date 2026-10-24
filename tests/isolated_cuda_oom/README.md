# Изолированная проверка controlled CUDA allocator OOM

Только операторский opt-in hook для `scripts/check-offline-restore.sh`. Это тестовые файлы, они не импортируются приложением и не меняют код модели или retrieval pipeline. Нельзя запускать против production generator или его GPU. Hook требует отдельный `korsik_offline_*` Compose project, GPU0 UUID, собственные secrets/volumes, отсутствие MAX и явный opt-in. Если production использует GPU0, этот hook применять нельзя.

```sh
KORSIK_ALLOW_OOM_HOOK=YES_AFTER_HTTP100 \
OFFLINE_CHECK_GPU_ID=0 \
OFFLINE_CHECK_OOM_HOOK="$PWD/tests/isolated_cuda_oom/run-cuda-oom-hook.sh" \
  scripts/check-offline-restore.sh /secure/backup/TIMESTAMP /secure/check-with-oom
```

Запускайте после завершения измерений производительности. Сначала обычный FULL cold proof, затем helper перезапускает только generator тестового проекта с внешней обёрткой: после загрузки неизменённой 4B модели allocator cap ограничивает свободный резерв. Настоящий запрос к decoder должен вызвать фактический `torch.cuda.OutOfMemoryError`; искусственная exception не инжектируется. Отдельная pump-нагрузка пересекается с реальными survey/capture/handoff/HR reply/dashboard commits. Затем cap снимается и реальный короткий generate должен восстановиться. Срок hook ограничен360с; недостаточный witness не является PASS. EXIT trap останавливает только тестовый generator/one-off helper, внешний restore trap удаляет проект и volumes.

Source15 actual:6 реальных OOM,14 проверок PASS, business commits1.926с, recoveryHTTP200. Из трёх RAG-вопросов один действительно дошёл до отказавшего generator; два нормативных вопроса выбрали literalEXTRACT без decoder. Исходные отчёты — `../cuda-oom-business-report-source15.json` и `../cuda-oom-observation-source15.json`. Это controlled allocator exhaustion, **не физическое исчерпание всей VRAM**, произвольный OOM или доказательство peak-нагрузки. Corporate quality acceptance остаётся отдельной.
