# 本地热点监控与待审核商品包

这是一个本地原型，不会自动登录闲鱼、不自动发布、不下载课程/视频/压缩包、不绕过站点限制，也不会自动把第三方资源搬运到百度网盘。

它目前支持 TheItzy 的公开 WordPress API：抓取文章标题、分类、发布时间、摘要、封面地址和来源页；按关键词和新鲜度打分；在 `output/<run_id>/` 生成待审核的 `copy.md`、`delivery.md`、`item.json` 和 `summary.json`。

1337x 抓取在代码中明确禁用。该站点常包含未授权影视/软件等资源，不能作为盗版资源的发现、搬运或售卖管线。要接第二个来源，请替换为你有权使用的官方 API、RSS 或授权素材库，并新增一个只返回公开元数据的适配器。

## 运行

```powershell
cd E:\ai\skills\xinli\resource_pipeline
python .\cli.py list
python .\cli.py run --task "AI数字产品选品整理"
python .\cli.py serve
```

然后打开 `http://127.0.0.1:8765/`。TheItzy 的 robots.txt 声明了 10 秒 crawl-delay，所以默认每次请求间隔 10 秒；不要通过并发、代理轮换或改 User-Agent 绕过限制。

本地整理任务会在页面“运行诊断”里显示抓取数、已处理跳过数、关键词命中数和最终输出数；容器日志也会打印每页抓取、筛选和完成状态。若输出为 0，优先看诊断里的 `zero_reason`，常见原因是本次抓到的文章都已经处理过，此时可以勾选“包含已处理”复查。

## 自动创建闲鱼热点监控任务

`goofish_tasks.json` 是批量任务配置，现在只保留 2 个低成本监控任务：1 个关键词快速筛选任务 + 1 个 AI 文本判断任务。两者都关闭图片分析（`analyze_images=false`），默认每 12 小时执行一次（`0 */12 * * *`）。同步器会先调用 `GET /api/tasks` 按任务名去重；AI 任务再调用 `POST /api/tasks/generate`、轮询 `/api/tasks/generate-jobs/{job_id}`，最后按需调用 `POST /api/tasks/start/{task_id}`。

先预览请求，不访问远端：

```powershell
python .\cli.py goofish-create --dry-run
```

确认配置后创建但不启动：

```powershell
python .\cli.py goofish-create
```

创建后立即启动新任务：

```powershell
python .\cli.py goofish-create --start
```

只同步某个任务：

```powershell
python .\cli.py goofish-create --task "AI数字产品核心热点" --start
```

默认 API 地址是 `https://goofish.xiaolicloud.cn:18443`，也可以通过 `.env` 中的 `GOOFISH_API_BASE_URL` 覆盖。当前接口按你贴出的说明没有额外 Token 校验；如果你在反向代理前加了鉴权，可填写 `GOOFISH_API_TOKEN`。不要把没有鉴权的管理接口直接暴露到公网。

## 让文案进入“可发布”状态

默认任务 `rights_confirmed=false`，输出会明确标记“禁止发布”。只有你确认商品内容有自有/正版分发权后，才把对应任务改成 `true`，再把你自己的网盘交付链接填入 `owned_delivery_links`。如需调用 OpenAI 兼容接口生成文案，复制 `.env.example` 为 `.env` 并填写 `AI_API_KEY`、`AI_BASE_URL`、`AI_MODEL`；文案仍需人工审核。

若还要在本地保存已获授权的封面，把任务的 `authorized_assets` 改为 `true`。程序只会下载与来源同域的图片，最多 8 MB，不下载视频、课程、压缩包或登录后内容。

## 输出结构

```text
output/
  state.json
  20260713-xxxxxx/
    summary.json
    01-标题/
      copy.md
      delivery.md
      item.json
      assets/       # 仅在 rights_confirmed + authorized_assets 时出现图片
```

## 后续 Docker / 百度网盘

当前先用本地目录验收。后续可以把 `output/` 挂载成卷，再接一个明确的百度网盘适配器：只上传你自己的授权文件，记录远端路径、文件哈希、上传时间和分享链接；不要把“来源页链接”当成“可交付下载链接”。

如果你在其他部署目录还有旧的 `docker-compose.yml`，其中的明文 API key 和后台密码应立即轮换，并改成 `.env` 或 Docker secrets；本项目的 Compose 文件只从环境变量读取可选凭据。

## Docker 部署

在本目录执行：

```powershell
Copy-Item .env.example .env
docker compose build
docker compose up -d
docker compose ps
Invoke-RestMethod http://127.0.0.1:8765/api/health
```

审核页面默认只绑定服务器本机 `127.0.0.1:8765`，可通过 SSH 隧道访问，或放在带登录保护的 Nginx Proxy Manager 后面。输出会持久化在当前目录的 `output/`。

Docker 中执行一次热点任务初始化/同步：

```powershell
docker compose --profile bootstrap run --rm goofish-bootstrap
```

查看日志和停止服务：

```powershell
docker compose logs -f pipeline
docker compose down
```

日志里重点看这些字段：

- `fetch_theitzy response page=... count=...`：TheItzy 每页实际返回多少条。
- `selection_done`：抓取数、跳过数、候选数、关键词命中数、最终输出数。
- `zero_reason`：输出 0 条时的直接原因。

`goofish-bootstrap` 默认会创建不存在的任务并启动新任务；重复执行会按任务名跳过已有任务。若只想查看提交内容，可在宿主机执行 `python .\cli.py goofish-create --dry-run`，避免误调用远端 API。远端已经存在的旧重复任务不会被自动删除或停止，需要在 Web 控制台或闲鱼监控后台里手动处理。
