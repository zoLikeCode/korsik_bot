# Российский HTTPS ingress для Корсика

Состояние на 24 октября 2026: TLS установлен на **https://app.korsik-demo.ru**; стабильность внешнего HTTPS-маршрута пока не подтверждена. На VPS `212.57.127.220` установлен отдельный domain vhost Nginx; публичный сертификат Let’s Encrypt выдан только для `app.korsik-demo.ru` и действует до 22 января 2027. TLS 1.2/1.3, полная цепочка CA/hostname, HTTP→HTTPS, ACME path, все 23 текущих HTML/JS/CSS файла и отказ неавторизованной сессии проверены. См. [HTTPS-протокол](../../tests/ru-ingress-https-report.json), [успешную проверку продления](../../tests/ru-ingress-renewal-report.json) и [SSH-протокол](../../tests/ru-ingress-tunnel-report.json).

Действующая цепочка: `app.korsik-demo.ru:443 → VPS Nginx → 127.0.0.1:18080 → отдельный SSH forward → Vast 127.0.0.1:8080`. Прежние 20 файлов ISPmanager/Nginx сохранены без изменений; добавлен только собственный vhost `/etc/nginx/sites-available/app.korsik-demo.ru`. Certbot timer включён, domain-scoped deploy-hook проверяет `nginx -t` перед reload; реальный staging `renew --dry-run --run-deploy-hooks` завершился с кодом 0. На edge HTTP приложение не выдаётся: разрешены ACME и redirect; установлен HSTS `max-age=86400` без `includeSubDomains`/`preload`. Текущий edge body limit — 52 MiB, отдельный rate limit из общего шаблона не устанавливался.

Зафиксирована временная проблема внешнего маршрута: после первого успешного CA/SNI HTTPS GET некоторые запросы с Mac останавливались до HTTP на TLS handshake; с VPS и Vast HTTPS продолжал работать. Позже root получил публичный `HTTP 200`, `ssl_verify_result=0` за 0,364 с без изменения конфигурации; затем внешний TLS timeout повторился. Проверки auth/cookies с Vast проходят, а часть запросов с Mac завершается до HTTP. Причина сетевого эпизода пока не установлена. Это не основание заявлять непрерывную доступность из любой сети. Реальный browser-login и настоящая MAX MiniApp-проверка ведутся root отдельно; эти HTTPS/SSH-протоколы не подтверждают их автоматически.

Для корня `korsik-demo.ru` сертификат не заказывался: первоначально authoritative AAAA вёл на парковку Timeweb. Рабочий hostname выбран `app.korsik-demo.ru` с A на VPS и без AAAA. Не использовать apex вместо точного HTTPS URL приложения до отдельной DNS/TLS-проверки. Команды ниже относятся к повторному операторскому развёртыванию; существующие аккаунты, ключи и сертификат повторно создавать не нужно.

Назначение — небольшой Ubuntu 24.04 / Debian 12 VPS с выделенным статическим IPv4. Nginx принимает HTTPS на обычном TCP/443; SSH-клиент на VPS создаёт закрытый локальный forward к существующему web gateway на Vast. Локальные модели и документы продолжают работать на Vast. Cloudflare не используется.

```text
MAX / браузер → https://app.korsik-demo.ru:443 → nginx на VPS РФ
                                     ↓ 127.0.0.1:18080
                            SSH к 103.119.82.252:28902
                                     ↓ зашифрованный канал
                            Vast 127.0.0.1:8080 → web/API
```

Это не HA: доступ зависит от российского VPS, SSH-сети и существующего узла/инстанса Vast. systemd восстанавливает соединение, но не заменяет отказавший Vast. ACME требует сетевого обращения Certbot к удостоверяющему центру при выпуске/продлении; приложение не обращается к внешней LLM и не использует внешний сервис авторизации.

## Состав пакета и историческая проверка

- `nginx-http.conf.template` — только HTTP-проверка домена для первого сертификата.
- `nginx-https.conf.template` — TLS, ACME-продление, прозрачный reverse proxy.
- `korsik-vast-tunnel.service` — OpenSSH под отдельным локальным пользователем VPS.

В Nginx-шаблонах заменяется только `__DOMAIN__` на один реальный ASCII FQDN без схемы/пути/порта. Шаблоны не содержат сертификатов, закрытых ключей, паролей или токена бота. Ключ существующего root-доступа **никогда не копировать на VPS**.

Первоначальная локальная проверка до покупки сохранена в `validation.json` как исторический протокол: на машине автора отсутствовали Nginx, Certbot, systemd и Docker daemon. Фактические `sshd -t/-T`, `systemd-analyze verify` и работа туннеля на VPS уже подтверждены отдельным протоколом выше. Фактические Nginx/TLS проверки теперь записаны в отдельном HTTPS-протоколе выше. Общие шаблоны ниже не являются побайтовой копией текущего domain vhost: фактически установленный hostname — app.korsik-demo.ru, access-log выключен, error-log имеет уровень crit, лимит тела 52 MiB.

## 1. Подготовить VPS и отдельный ключ

Дальше все команды — на новом выделенном VPS РФ, с root/sudo. Выбрать голую ОС без платной панели, бэкапов или иных услуг. TCP/80 и 443 должны быть доступны снаружи; SSH администратора разрешается отдельно. Не отключать действующий SSH/firewall-сеанс.

```bash
apt-get update
apt-get install --yes nginx certbot openssh-client ca-certificates openssl
useradd --system --create-home --user-group --home-dir /var/lib/korsik-ingress --shell /usr/sbin/nologin korsik-tunnel
install -d -o korsik-tunnel -g korsik-tunnel -m 0700 /var/lib/korsik-ingress/ssh
install -d -o root -g root -m 0755 /etc/korsik-ingress
runuser -u korsik-tunnel -- ssh-keygen -t ed25519 -N '' -C korsik-ingress-forward-only -f /var/lib/korsik-ingress/ssh/id_ed25519
cat /var/lib/korsik-ingress/ssh/id_ed25519.pub
```

Генерация выполняется один раз. Если пользователь/файл уже существует — сначала проверить его происхождение; не перезаписывать ключ и не менять владельца чужих файлов. Передать **только публичный** `.pub` на Vast по существующему доверенному администраторскому каналу.

## 2. Подготовить ограниченную учётную запись на Vast

Использовать существующий доверенный root-сеанс к Vast. Сначала определить **реальный sshd**, обслуживающий mapped port 28902: его процесс, внутренний listener, путь `sshd`, аргумент `-f` и механизм запуска. Drop-in работает только если реальный конфиг содержит соответствующий `Include`. Не считать, что произвольный `/etc/ssh/sshd_config.d` автоматически подключён. Не перезапускать контейнер/VM и не выполнять `killall sshd`.

Создать отдельную учётную запись `korsik-edge`, если её ещё нет. `/bin/false` и приведённый Match запрещают интерактивный доступ. Чтобы публичный ключ не отклонялся из-за заблокированного аккаунта на отдельных PAM-конфигурациях, задать аккаунту случайный неизвестный пароль, а SSH-пароли запретить в Match. Ничей существующий пароль не изменяется.

```bash
useradd --create-home --user-group --shell /bin/false korsik-edge
# Только новый korsik-edge; случайный пароль не выводится и не сохраняется.
{ printf 'korsik-edge:'; openssl rand -base64 48 | openssl passwd -6 -stdin; } | chpasswd -e
install -d -o korsik-edge -g korsik-edge -m 0700 /home/korsik-edge/.ssh
```

В `/home/korsik-edge/.ssh/authorized_keys` поместить одну строку с **публичным ключом, созданным на VPS**; заменить `EDGE_IPV4` на его статический IPv4:

```text
from="EDGE_IPV4",restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 AAAA... korsik-ingress-forward-only
```

```bash
chown korsik-edge:korsik-edge /home/korsik-edge/.ssh/authorized_keys
chmod 0600 /home/korsik-edge/.ssh/authorized_keys
```

Включить в фактически используемый sshd config следующий блок, ограниченный **только новым пользователем**. Сохранить резервную копию конфига до правки. `Match all` завершает область ограничений.

```text
Match User korsik-edge
    AuthenticationMethods publickey
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    X11Forwarding no
    AllowAgentForwarding no
    AllowTcpForwarding local
    AllowStreamLocalForwarding no
    PermitOpen 127.0.0.1:8080
    PermitListen none
    GatewayPorts no
    PermitUserRC no
    MaxSessions 0
    ForceCommand /bin/false
Match all
```

`ssh -N` не открывает shell/session channel; поэтому работает с `MaxSessions 0` и `ForceCommand /bin/false`. Разрешён только TCP local forwarding к точному `127.0.0.1:8080`; shell/SFTP, remote forwarding, Unix-socket forwarding, agent forwarding и другие назначения запрещены.

Перед reload выполнить **для фактического бинарника и конфига**:

```bash
/usr/sbin/sshd -t -f /etc/ssh/sshd_config
/usr/sbin/sshd -T -f /etc/ssh/sshd_config -C user=korsik-edge,host=localhost,addr=EDGE_IPV4
```

Это примеры путей: если процесс использует другой `-f`, подставить именно его. Проверить effective `allowtcpforwarding local`, `permitopen 127.0.0.1:8080`, `allowstreamlocalforwarding no`, `maxsessions 0`, `forcecommand /bin/false`, `passwordauthentication no`, `disableforwarding no`. Последний глобальный запрет, если включён, блокирует и этот туннель. Сравнить effective-конфигурацию root до/после: она не должна меняться.

Reload делать только через фактическую службу/процесс listener после успешного `-t`; сохранить текущий root-сеанс и проверить новый администраторский сеанс. Для стандартной Ubuntu/Debian-службы это `systemctl reload ssh`. Если Vast запускает sshd иначе, эту команду не применять наугад.

## 3. Закрепить доверенный host key Vast

Через уже доверенный root-сеанс на Vast прочитать **публичный** host key реального sshd (путь видно в `sshd -T`, обычно `/etc/ssh/ssh_host_ed25519_key.pub`) и его fingerprint через `ssh-keygen -lf ...pub`. Передать публичную строку и fingerprint оператору VPS по доверенному каналу.

На VPS создать `/etc/korsik-ingress/known_hosts`:

```text
[103.119.82.252]:28902 ssh-ed25519 AAAA...ДОВЕРЕННЫЙ_HOST_PUBLIC_KEY...
```

```bash
chown root:root /etc/korsik-ingress/known_hosts
chmod 0644 /etc/korsik-ingress/known_hosts
ssh-keygen -lf /etc/korsik-ingress/known_hosts
```

Сверить fingerprint с полученным по доверенному каналу. Один `ssh-keyscan` без независимой сверки не доказывает подлинность. Не использовать `StrictHostKeyChecking=no` / `accept-new`. При замене host key Vast остановить туннель, проверить новый ключ по доверенному каналу и только затем обновить pin.

## 4. Проверить и включить туннель

Скопировать только unit-файл пакета на VPS. Закрытый edge key остаётся на VPS.

```bash
install -o root -g root -m 0644 korsik-vast-tunnel.service /etc/systemd/system/korsik-vast-tunnel.service
systemd-analyze verify /etc/systemd/system/korsik-vast-tunnel.service
systemctl daemon-reload
systemctl enable --now korsik-vast-tunnel.service
systemctl status --no-pager korsik-vast-tunnel.service
ss -lntp
curl --fail --max-time 15 -H 'Host: app.example.ru' http://127.0.0.1:18080/
```

Подставить реальный домен в Host. Проверить listener **только `127.0.0.1:18080`**, доступность полного HTML/JS через него и отсутствие нового публичного порта. `active`/`ExitOnForwardFailure` доказывает наличие локального listener, но не доступность Vast HTTP — поэтому `curl` обязателен. Ошибка host key/публичного ключа не обходится ослаблением SSH-проверок.

## 5. DNS, HTTP и первый сертификат

У существующего DNS-провайдера добавить A-запись выбранного домена на IPv4 VPS. Не добавлять AAAA без настроенного IPv6. Регистратор домена не обязан совпадать с VPS-провайдером. Перед продолжением оператор проверяет DNS с внешней сети.

На VPS подготовить файлы из шаблонов в отдельном каталоге, заменив `__DOMAIN__`. Это не устанавливает их:

```bash
EDGE_DOMAIN=app.example.ru
export EDGE_DOMAIN
python3 - <<'PYRENDER'
import os, re
from pathlib import Path
name = os.environ['EDGE_DOMAIN']
assert len(name) <= 253 and re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}', name), 'Use one lowercase ASCII DNS name'
out = Path('/root/korsik-ingress-rendered')
out.mkdir(mode=0o700, exist_ok=False)
for source in ('nginx-http.conf.template', 'nginx-https.conf.template'):
    (out/source.removesuffix('.template')).write_text(Path(source).read_text().replace('__DOMAIN__', name))
PYRENDER
install -d -o root -g root -m 0755 /var/www/korsik-acme/.well-known/acme-challenge
```

Установка HTTP stage: на **новом выделенном VPS** убрать только стандартный nginx welcome-site из `sites-enabled`, сохранив резервную копию. Не удалять неизвестные сайты. Установить один активный файл:

```bash
cp -a /etc/nginx/sites-enabled /root/nginx-sites-enabled-before-korsik
rm /etc/nginx/sites-enabled/default
install -m 0644 /root/korsik-ingress-rendered/nginx-http.conf /etc/nginx/sites-available/korsik-ingress
ln -s /etc/nginx/sites-available/korsik-ingress /etc/nginx/sites-enabled/korsik-ingress
nginx -t
systemctl reload nginx
printf 'korsik-acme-probe\n' > /var/www/korsik-acme/.well-known/acme-challenge/probe
```

С внешней машины проверить `http://ДОМЕН/.well-known/acme-challenge/probe` → точное `korsik-acme-probe`. В текущей установке пользователь разрешил выпуск сертификата без предоставленного email; применены стандартное согласие с условиями CA и `--register-unsafely-without-email`. Далее дан пример для будущего оператора с его собственным email. Затем на VPS выпустить сертификат. `--agree-tos` ниже намеренно отсутствует: оператор сначала знакомится с условиями CA и сам принимает их в интерактивном Certbot.

```bash
certbot certonly --webroot -w /var/www/korsik-acme -d "$EDGE_DOMAIN" --email operator@example.ru --cert-name "$EDGE_DOMAIN"
install -m 0644 /root/korsik-ingress-rendered/nginx-https.conf /etc/nginx/sites-available/korsik-ingress
nginx -t
systemctl reload nginx
```

Подставить реальную почту оператора. Если Certbot не выпустил сертификат — HTTPS stage не включать. Оба шаблона одновременно не включаются. Nginx minimum: 1.19.4 (`ssl_reject_handshake`), штатные Ubuntu 24.04/Debian 12 подходят.

## 6. HTTPS-схема внутри origin и приложение

Edge сохраняет `$http_host`, передаёт `X-Forwarded-Proto: https`, не переписывает cookie и не выдаёт браузеру URL Vast. **Текущий gateway на Vast в `/api/` перезаписывает заголовок на `$scheme` (= `http`)**. В этом развёртывании origin Nginx не менялся, сохранение HTTPS-схемы до ASGI не заявляется. Приложение использует проверенные `PUBLIC_URL=https://app.korsik-demo.ru` и `SECURE_COOKIES=true`; сервер MiniApp допускает этот защищённый публичный контур согласно своей существующей проверке. См. [фактический cutover](../../tests/ru-ingress-cutover-report.json). Нельзя считать произвольный клиентский X-Forwarded-Proto доверенным. Приведённая далее map — отдельный вариант изменения origin после проверки peer и operator approval; она сейчас не установлена.

Перед отдельным изменением доверия к forwarded headers убедиться, что Vast web port 8080 опубликован только на loopback и недоступен через публичный mapped HTTP port. Затем определить фактический `$remote_addr` gateway для запроса через SSH (в Docker это может быть bridge gateway, а не `127.0.0.1`). Только этот подтверждённый peer разрешается доверять. В `http {}` origin gateway добавить map, заменив `TRUSTED_DOCKER_GATEWAY_IP` на проверенный IP:

```nginx
map "$remote_addr:$http_x_forwarded_proto" $korsik_forwarded_proto {
    default $scheme;
    "127.0.0.1:https" https;
    "TRUSTED_DOCKER_GATEWAY_IP:https" https;
}
```

В существующем origin `/api/` заменить ровно `proxy_set_header X-Forwarded-Proto $scheme;` на `proxy_set_header X-Forwarded-Proto $korsik_forwarded_proto;`. Не включать доверие ко всем публичным входящим X-Forwarded-Proto. Проверить origin `nginx -t` и reload под контролем оператора; до этого шага не заявлять сохранение HTTPS-схемы до API.

В существующем Compose `.env` согласовать реальные входные переменные `PUBLIC_URL=https://ДОМЕН`, `SECURE_COOKIES=true` и, для стабильного webhook, `MAX_PUBLIC_URL=https://ДОМЕН`. `compose.yaml` уже преобразует первые две в `KORSIK_PUBLIC_URL` / `KORSIK_SECURE_COOKIES`; добавлять только `KORSIK_*` в `.env` недостаточно. Проверить допустимый origin/Host в текущей серверной конфигурации и фактические proxy/trust настройки API. Сохранить существующие секреты и ключ сессий. Операторские изменения приложения и webhook выполняются отдельно после проверки; в пакете нет MAX token и никакой автоматической перерегистрации бота.

## 7. Проверки cutover и продление

До переключения основного адреса проверить с внешней машины:

- TLS-сертификат совпадает с доменом, цепочка доверенная, без `curl -k`; HTTP → HTTPS; чужой Host не обслуживается.
- Полный HTML и каждый JS/CSS asset скачиваются, без прежней остановки на 16 КБ; размеры/SHA соответствуют текущей сборке.
- Реальный вход → `/auth/session`, CSRF-защищённое сохранение, повторное открытие страницы; cookie `Secure`/`HttpOnly`/SameSite и host-only domain сохранены; нет редиректов на Vast/loopback.
- Точечная проверка роли/источника и загрузки файла, исходный путь/query и `Host` не потеряны. Edge ceiling — 100 MiB, но лимиты приложения/внутреннего gateway (сейчас 52 MiB) продолжают действовать.
- `/internal/` недоступен снаружи. Bot webhook проверяет прежний секрет; не отправлять реальные сообщения и не менять bindings без отдельной авторизации.
- Остановка тестового туннеля даёт ошибку upstream, восстановление поднимается systemd; по этому тесту не делать вывод о HA.

Для обновлений dashboard/API учитывается общий rate 60 req/s на IP, burst 120; для крупной корпоративной NAT-сети лимит нужно пересмотреть по нагрузке. Кэш ответов/сессий на edge выключен. Access-log не содержит URL/query, тела, cookie или initData.

Установить root-owned deploy-hook (после выпуска сертификата), затем проверить таймер:

```bash
install -d -o root -g root -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/korsik-nginx <<'HOOK'
#!/bin/sh
set -eu
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
HOOK
chmod 0750 /etc/letsencrypt/renewal-hooks/deploy/korsik-nginx
systemctl enable --now certbot.timer
systemctl list-timers certbot.timer
certbot renew --dry-run --run-deploy-hooks
```

HTTP ACME path продолжает работать после включения TLS и не зависит от туннеля. Только после реального успешного HTTPS/renewal smoke можно отдельно включить HSTS с коротким первым `max-age=86400`, **без** `includeSubDomains` и `preload`; до проверки он отсутствует.

При проблеме cutover сохранить прежний рабочий адрес, вернуть предыдущую DNS/публичную конфигурацию, остановить `korsik-vast-tunnel`, отозвать только его отдельный ключ/пользователя после проверки. Не удалять данные, текущие root-ключи или действующую привязку MAX. Сертификаты, ключи и реальные DNS/IP-параметры не коммитить в репозиторий.
