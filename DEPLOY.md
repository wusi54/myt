# 云phone设备管理平台 V2.1.0 — Docker 部署说明

本项目使用 **单镜像、双服务** 的方式部署：

| 服务 | 说明 | 端口 |
| --- | --- | --- |
| `web`   | Flask 应用（waitress 托管）：设备/容器/真机参数/代理池管理 | `9111` |
| `relay` | 投屏中继（WebSocket + `adb screenrecord`），群控窗实时画面 | `9000` |

两个服务使用同一个镜像 `myt-manager:2.1.0`，镜像内已安装 `adb`。

---

## 一、前置条件

- Docker Desktop / Docker Engine + Docker Compose v2+
- **Linux 容器**模式（Windows 上需开启 Docker Desktop 的 Linux containers）
- 宿主机能访问设备/盒子所在网段（容器通过 NAT 出去）
- 浏览器只需访问宿主机 `9111`（由 `nginx` 统一 TLS 终止并反代 Flask 与投屏中继 `9000`；`9000` 为容器内部端口，不再对外暴露）
- 宿主机需提供 `./certs/server.crt` 与 `./certs/server.key`（自签或 CA 证书；`certs/` 已在 `.gitignore` 中、不入库，部署时自行放置）

---

## 二、快速开始

```bash
# 1. 构建并后台启动
docker compose up -d --build

# 2. 查看状态（web 显示 healthy 即正常）
docker compose ps

# 3. 查看日志
docker compose logs -f

# 4. 访问
#    Web 控制台：http://<宿主机IP>:9111  （首次访问跳转登录页）
#    投屏页面随群控窗自动使用 :9000
```

> **默认管理员账号：`admin` / `admin123`** — 首次登录后请立即在「用户管理」中修改密码。
> 首次启动若库中没有任何用户，会自动创建该管理员。

停止 / 重启 / 更新：

```bash
docker compose down            # 停止并删除容器（数据卷保留）
docker compose restart         # 重启
docker compose up -d --build   # 拉取最新代码后重建
```

---

## 三、数据持久化（重要）

容器内的运行期数据全部通过 **卷挂载** 落到宿主机目录，删除/重建容器不会丢数据：

| 宿主机目录 | 容器路径 | 内容 |
| --- | --- | --- |
| `./data/`         | `/app/data`         | SQLite 数据库 `data.db`（含 `-wal`/`-shm`） |
| `./uploads/`      | `/app/uploads`      | 上传的真机参数文件 |
| `./app_uploads/`  | `/app/app_uploads`  | 上传的 APK 文件 |

**首次启动**：`docker-entrypoint.sh` 会把镜像内置的「种子数据库」（构建时的 `data.db`）复制到 `./data/data.db`。
之后所有变更都写入 `./data/data.db`，与种子库互不影响。

> 如需从空库开始：`docker compose down && rm -rf ./data && docker compose up -d`。

---

## 四、环境变量

可在 `docker-compose.yml` 的 `environment` 或 `.env` 中覆盖：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH`         | `/app/data/data.db`     | SQLite 数据库路径 |
| `UPLOAD_DIR`      | `/app/uploads`          | 真机参数文件目录 |
| `APP_UPLOAD_DIR`  | `/app/app_uploads`      | APK 目录 |
| `ADB_PATH`        | `/usr/bin/adb`          | adb 可执行文件路径 |
| `PORT`            | `9111`                  | Web 服务端口 |
| `RELAY_PORT`      | `9000`                  | 投屏中继端口 |
| `SECRET_KEY`      | `change-me-in-production` | JWT 签名密钥，**生产务必修改**；修改后已登录用户需重新登录 |
| `TOKEN_TTL`       | `604800`（7 天）         | 登录 token 有效期（秒） |
| `THREADS`         | `24`                    | waitress 线程数（并发请求） |
| `TZ`              | `Asia/Shanghai`         | 时区 |

> 提示：`PORT`/`RELAY_PORT` 改端口时，需同步修改 `docker-compose.yml` 的 `ports` 映射与命令中的 `--port`。

---

## 五、登录与用户管理

平台采用 **JWT 鉴权**，两级角色：

| 角色 | 权限 |
| --- | --- |
| 管理员 `admin` | 全部功能 + 用户管理（增删改、重置密码、启用/禁用） |
| 普通用户 `user` | 登录后使用设备/容器/真机参数/代理池等业务功能，**不可进入用户管理** |

- 未登录访问任何页面 → 自动跳转 `/login`；访问受保护 API → 返回 `401`。
- 登录成功后浏览器写入 HttpOnly Cookie（`myt_token`），前端请求自动携带；接口也支持 `Authorization: Bearer <token>`（便于后续对接第三方客户端）。
- 顶部导航右侧显示当前用户，可「修改密码」「退出」。
- **安全保护**：不能删除当前登录账号；始终保留至少一个可用的管理员账号（否则拒绝降级/禁用/删除）。
- 投屏中继（`9000`）当前**不鉴权**，仅 `9111` 的页面与 API 受保护。

首次启动自动创建默认管理员：**admin / admin123**。

---

## 六、常见问题

### 1. 容器内无法访问设备 / 盒子
容器默认走 Docker 的 NAT 网络。请确认宿主机能 `ping`/`curl` 到设备地址。
若设备只允许宿主机本机访问（例如设备与宿主同网段但容器网段不可达），可改用 host 网络：

```yaml
  web:
    network_mode: host      # 此时 ports 映射失效，直接用宿主端口
    environment:
      PORT: "9111"
```

> Windows / macOS 的 Docker Desktop 对 `network_mode: host` 支持有限，Linux 宿主推荐此方式。

### 2. 投屏黑屏 / 连接失败
- 确认 `relay` 容器在运行：`docker compose ps`
- 确认设备已开启 adb 网络调试，且容器内能连上设备：
  ```bash
  docker compose exec relay adb connect <设备IP>:<端口>
  docker compose exec relay adb devices
  ```
- 浏览器只需能访问宿主机 `9111`（由 nginx 反代 `/ws/screen` → relay `9000`，防火墙放行 `9111` 即可，`9000` 无需对外）。

### 3. adb 需要授权
部分设备首次 `adb connect` 需要在设备端确认授权。可在 `relay` 容器内手动执行一次 `adb connect`，设备端确认后即可。

### 4. 端口被占用
修改 `docker-compose.yml` 中 `ports` 左侧宿主端口即可，例如 `"15001:9111"`。

### 5. 镜像体积较大
真机参数文件（`uploads/`）与 APK（`app_uploads/`）已在 `.dockerignore` 中排除，不打进镜像，运行期由卷挂载提供，镜像仅含代码 + adb。

---

## 七、不使用 Docker 时的本地运行

```bash
pip install -r requirements.txt

# Web（等价于旧版 python app.py；如需热重载加 DEBUG=1）
python app.py

# 投屏中继（另开一个终端）
python screen_relay.py
```

Windows 下 `ADB_PATH` 默认指向 Android SDK 的 `adb.exe`；其他平台设为实际路径或 `adb`。

---

## 八、CentOS / RHEL 宿主机注意事项

镜像内是 **Debian（`python:3.11-slim`）**，apt/pip 都在容器里执行，**与宿主机的 CentOS 无关**。但宿主机（CentOS）上有几处需要确认：

### 1. Docker 与 Compose 版本
本 `docker-compose.yml` 使用 **Compose 规范**（无 `version:` 字段，含 `x-app` 扩展与 `<<:` 锚点），需要 **Docker Compose v2**：
```bash
docker compose version        # 需要 v2，输出形如 Docker Compose version v2.x
```
若只有旧版 `docker-compose`（v1），请升级安装 `docker-ce` + `docker-compose-plugin`（CentOS 7 建议 7.9）。

### 2. SELinux（重点）
CentOS 默认 **SELinux enforcing**，bind mount 的宿主目录容器可能无权限访问。本仓库卷挂载已加 `:z`（见 `docker-compose.yml`）。若仍报 `Permission denied`：
```bash
getenforce                    # 确认是否 Enforcing
# 方案A：改用私有标签 :Z（把 :z 改成 :Z）
# 方案B：手工打标签
chcon -Rt svirt_sandbox_file_t ./data ./uploads ./app_uploads
```

### 3. 防火墙放行端口
```bash
firewall-cmd --permanent --add-port=9111/tcp
firewall-cmd --permanent --add-port=9000/tcp
firewall-cmd --reload
```

### 4. 能否拉取基础镜像（Docker Hub 可达性）
构建需要拉取 `python:3.11-slim`。若 live 环境访问 Docker Hub 受限，配置镜像加速器（阿里云）到 `/etc/docker/daemon.json`：
```json
{ "registry-mirrors": ["https://<你的ID>.mirror.aliyuncs.com"] }
```
```bash
systemctl restart docker
```

### 5. 推荐：完全离线部署（最稳）
若 live 环境网络受限（如无法访问 Docker Hub / deb.debian.org），可**在能联网的机器上构建好镜像**，再离线导入：

```bash
# ① 能联网的机器：构建并导出镜像
docker compose build
docker save myt-manager:2.1.0 -o myt-manager-2.1.0.tar
gzip myt-manager-2.1.0.tar        # 可选，文件较大

# ② 把 myt-manager-2.1.0.tar.gz 和本仓库一起拷到 live 机器

# ③ live 机器：导入镜像后直接启动（不要加 --build，避免联网重新构建）
docker load -i myt-manager-2.1.0.tar.gz
docker compose up -d
```
