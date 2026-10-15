# 云phone设备管理平台 V2.1.0 — Docker 部署说明

本项目使用 **单镜像、双服务** 的方式部署：

| 服务 | 说明 | 端口 |
| --- | --- | --- |
| `web`   | Flask 应用（waitress 托管）：设备/容器/真机参数/代理池管理 | `5001` |
| `relay` | 投屏中继（WebSocket + `adb screenrecord`），群控窗实时画面 | `9000` |

两个服务使用同一个镜像 `myt-manager:2.1.0`，镜像内已安装 `adb`。

---

## 一、前置条件

- Docker Desktop / Docker Engine + Docker Compose v2+
- **Linux 容器**模式（Windows 上需开启 Docker Desktop 的 Linux containers）
- 宿主机能访问设备/盒子所在网段（容器通过 NAT 出去）
- 浏览器能访问宿主机 `5001`、`9000` 端口

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
#    Web 控制台：http://<宿主机IP>:5001  （首次访问跳转登录页）
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
| `PORT`            | `5001`                  | Web 服务端口 |
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
- 投屏中继（`9000`）当前**不鉴权**，仅 `5001` 的页面与 API 受保护。

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
      PORT: "5001"
```

> Windows / macOS 的 Docker Desktop 对 `network_mode: host` 支持有限，Linux 宿主推荐此方式。

### 2. 投屏黑屏 / 连接失败
- 确认 `relay` 容器在运行：`docker compose ps`
- 确认设备已开启 adb 网络调试，且容器内能连上设备：
  ```bash
  docker compose exec relay adb connect <设备IP>:<端口>
  docker compose exec relay adb devices
  ```
- 浏览器需能访问宿主机 `9000` 端口（防火墙放行）。

### 3. adb 需要授权
部分设备首次 `adb connect` 需要在设备端确认授权。可在 `relay` 容器内手动执行一次 `adb connect`，设备端确认后即可。

### 4. 端口被占用
修改 `docker-compose.yml` 中 `ports` 左侧宿主端口即可，例如 `"15001:5001"`。

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
