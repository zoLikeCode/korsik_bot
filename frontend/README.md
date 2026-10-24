# Кабинет Корсика

React 19, TypeScript, Vite и React Flow (MIT core). Все ресурсы собираются локально; runtime не использует CDN или внешние AI API.

```sh
npm ci --registry=https://registry.npmjs.org
npm run dev
```

Vite направляет `/api` в локальный backend на `127.0.0.1:8000`. Production: `npm run build`, статическая сборка в `dist/`; Dockerfile собирает её и запускает nginx от непривилегированного пользователя на 8080. Проксирование API идёт в Compose-сервис `api:8000`. SPA routing поддерживает перезагрузку вложенного URL.

Проверки: `npm run build` (TypeScript и production bundle), `npm test` (семантическая вставка/удаление узлов без изменения исходных IDs/source mappings), `npm audit --registry=https://registry.npmjs.org`.

Сессия и CSRF управляются backend. Frontend использует cookie credentials, `X-CSRF-Token`, `If-Match` и UUID для команд. Ошибки API выводятся пользователю; серверные списки не подменяются демонстрационными данными. Аккаунты и токены в код не входят.
