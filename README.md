# 线上展馆 API：观众参观路径 + 高清展品图切片

两大能力：

1. 从浏览器上报的 `open` / `close` 事件还原匿名观众的实际参观路径，避免把未关闭
   页面计作完整参观；支持乱序补报、重传去重与数据同意撤回。
2. 按展品号上传 PNG/JPEG 原图，生成逐层缩小至单块的 256 像素瓦片金字塔与清单，
   供浏览器按倍率/视口按需取图；支持原子换图、历史修订可读与故障重启恢复。

技术栈：Python 3.11 + FastAPI + SQLite（WAL 模式）+ Pillow，Docker Compose 一键启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档：
#   Swagger UI  http://localhost:8000/docs
#   ReDoc       http://localhost:8000/redoc
# 健康检查     http://localhost:8000/health
```

SQLite 数据文件与瓦片/原图均存放在命名卷 `exhibition-data`
（容器内 `/data/exhibition.db` 与 `/data/tiles`），容器重建不丢数据。

本地（无 Docker）运行：

```bash
pip install -r requirements.txt
EXHIBITION_DB=./exhibition.db EXHIBITION_TILE_ROOT=./tiles \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/events` | 上报单个事件 |
| POST | `/events/batch` | 批量上报，单事务原子提交 |
| GET | `/visitors/{visitor_id}/path` | 查询观众参观路径 |
| POST | `/visitors/{visitor_id}/withdraw` | 撤回数据使用同意（body 可省略，或传 `{"visitor_id": "..."}`） |
| POST | `/exhibits/{exhibit_id}/images` | 上传展品原图（PNG/JPEG），生成并发布 256 px 瓦片 |
| GET | `/exhibits/{exhibit_id}/manifest` | 查询现役切片清单（可加 `?revision=N` 读历史修订） |
| GET | `/exhibits/{exhibit_id}/revisions` | 查询修订历史与构建/现役状态 |
| GET | `/exhibits/{exhibit_id}/tiles/{revision}/{level}/{x}/{y}` | 按修订号与坐标取瓦片图 |
| GET | `/health` | 健康检查 |

事件字段：`event_id`（全局唯一，重传复用同一号）、`visitor_id`（匿名观众号）、
`page_view_id`（页面浏览号，同一次浏览的开关共享）、`exhibit_id`（展品号）、
`event_type`（`open` / `close`）、`event_time`（**带时区的 UTC ISO-8601**，
如 `2026-11-18T08:00:00Z`；无时区时间戳返回 400）。

### 上报响应语义

- `stored`：首次入库
- `duplicate`：`event_id` 已存在，幂等忽略（即使重传内容被篡改，也以首次内容为准）
- `rejected_withdrawn`：该观众已撤回同意，事件不入库，路径不会因此恢复

HTTP 状态码：成功受理固定 `201`，具体结果看响应体 `status`；参数/时间非法为
`422` / `400`。

### 路径响应

按 `page_view_id` 配对开关，同一观众按 **open 时间** 排序（`sequence` 从 1 连续编号）：

- 有关闭：`dwell_known=true`，`dwell_seconds` 为停留秒数
- 缺关闭：仍计为一次参观，但 `closed_at=null`、`dwell_known=false`、`dwell_seconds=null`（停留未知）
- 只有 close 没有 open 的孤儿事件不产生参观项
- close 早于 open 的异常数据：保留关闭时间，但停留时长标记未知

## 高清展品图：原图上传与切片发布

浏览器按缩放倍率与视口按需取瓦片，避免一次性加载整张大图。

`POST /exhibits/{exhibit_id}/images` 以 `multipart/form-data` 上传字段 `file`
（仅接受 PNG / JPEG，否则 `415`；空文件 `400`；展品号限字母数字与 `._-`）。
服务端构建图像金字塔：

- 瓦片边长固定 `256` 像素；**层级 0 是把整张图缩到一块瓦片内的最小层**，
  逐层放大 2 倍，最深层保持原图分辨率。
- 每层尺寸按向上取整 halving 计算，因此**边缘瓦片直接在图像边界裁切**，
  记录实际宽高（可能小于 256），不补白、不丢任何画面。
- 清单 `manifest` 给出原图 `width/height`、每个层级的尺寸与网格（`cols/rows`）、
  以及每块瓦片的 `level/x/y/width/height/url`。
- 清单与每个瓦片 URL 携带**同一来源修订号 `revision`**。

### 发布、换图与故障安全

1. **原子发布**：瓦片先写入 uuid 唯一的私有暂存目录，全部生成并逐文件校验后，
   才用一次原子 `os.replace` 落到发布路径；随后在**同一个 `BEGIN IMMEDIATE`
   事务**里写入完整清单并切换 `exhibits.active_revision`。客户端要么看到旧清单，
   要么看到指向完整文件集的新清单，不会看到半成品。
2. **换图旧版可读**：每次上传分配严格递增的新修订号，旧修订目录与清单保留；
   现役指针切换后，旧修订仍可用 `?revision=N` 查询并取图。
3. **构建失败不污染现役**：失败只把该修订标记为 `failed` 并清理自己的暂存目录，
   现役清单保持不动、继续可用；失败修订的清单/取图返回 404。
4. **早构建不能覆盖新原图**：激活前重新检查自己是否仍是最新修订；若上传期间
   已有更新的修订上线，则本次构建保留为完整但非现役的历史修订，接口返回 `409`。
5. **服务重启恢复**：构建行带属主 `owner_id` 与心跳（`build_owners` 表）。
   启动时仅把**属主心跳过期**（进程确已退出）的 `building` 行、以及文件缺失的
   `ready` 行标记为 `failed` 并清扫其专属目录，再按幸存的就绪修订重算现役指针。
   仍在心跳的属主（如第二个 worker 启动）的在建任务不受影响。清单因此永不指向
   缺失文件。

瓦片与清单存储在 `EXHIBITION_TILE_ROOT`（容器内 `/data/tiles`，与 SQLite 同处
命名卷）。平面布局为 `<root>/<展品号>__rev<修订号>/{manifest.json,original,tiles/...>`，
每个修订是互不嵌套的唯一目录，避免并发构建在共享父目录上相互干扰。

## 设计要点（如何满足需求）

1. **重传不重复计数**：`events.event_id` 为主键；插入前先查存在性，存在即返回
   `duplicate`，整段逻辑在同一事务内。
2. **乱序补报照常处理**：路径不做物化存储，每次查询从已提交事件实时配对、排序。
   后补的较早 open 会在下次查询时自然改变路径顺序；后补的 close 会把“停留未知”
   变成已知时长。
3. **撤回后不可恢复**：`withdrawals` 表持久化撤回标记；撤回在一个 `BEGIN IMMEDIATE`
   事务内完成「删除该观众全部事件 + 写撤回标记」。此后任何上传（包括旧事件重放）
   在同一事务内先查撤回标记，一律 `rejected_withdrawn`。
4. **交错操作基于同一已提交状态、不露半份路径**：
   - SQLite 开 WAL，写请求统一 `BEGIN IMMEDIATE` 立即拿写锁，配合 `busy_timeout`
     串行化所有写事务；
   - 读请求在单个 `BEGIN DEFERRED` 快照事务里完成「撤回检查 + 事件扫描」，
     要么看到撤回前的完整状态，要么看到撤回后的空路径；
   - 路径是查询时推导的，不存在“路径表更新一半”的中间态；
   - 批量上报为单事务，任一事件非法则整批回滚。
5. **线程/进程并发**：每请求一个独立 SQLite 连接（`check_same_thread=False`
   以适配 uvicorn 线程池，连接不在请求间共享）；多个 uvicorn worker 共享同一
   数据卷时由 SQLite 文件锁保证安全。

## 目录结构

```
app/
  config.py        环境变量配置（DB 路径、瓦片根目录、busy timeout）
  db.py            连接、schema、WAL/pragma、事务管理
  models.py        事件/路径 Pydantic 模型
  image_models.py  切片清单/修订 Pydantic 模型
  tiling.py        金字塔规划、瓦片渲染、暂存与文件校验
  image_crud.py    修订分配、原子激活、属主心跳与重启恢复
  crud.py          领域逻辑：入库去重、开关配对、路径推导、撤回
  main.py          FastAPI 路由与生命周期
tests/
  conftest.py
  test_api.py      事件/路径 15 项功能/并发测试
  test_tiles.py    切片 21 项：非整倍尺寸、换图、并发、故障、重启恢复
  stress_http.py   针对运行中服务的真实 HTTP 并发压测（手动运行）
Dockerfile
docker-compose.yml
```

## 自测

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

事件侧覆盖：正常配对与停留时长、乱序（close 先到）与更早事件补报导致的重排、
缺关闭标记未知及补关后转已知、孤儿 close、`event_id` 重传（含篡改内容）、
批量去重与整批原子回滚、撤回后路径消失/重复撤回幂等/旧事件重放与新事件均被拒、
多线程上传-查询-撤回交错（进程内 TestClient 与真实 HTTP 两种）、
无时区时间拒绝、时区偏移归一化、负停留异常、WAL 与主键的存储层校验。

切片侧覆盖：

- **非整倍尺寸**（300×100、1000×700、257×257、512×300、1×1 等）：层级 0 收敛到
  单块、最深层保持原分辨率，取全部最深层瓦片重拼后与原图逐像素一致（PNG），
  边缘瓦片尺寸正确、不丢画面；PNG 与 JPEG 两种输入。
- 清单几何、瓦片坐标、取图 200/404、错误坐标与未就绪修订、非图像/空文件/非法展品号。
- **换图**：旧修订持续可读、现役指针原子切换、修订历史状态与磁盘文件齐备。
- **并发换图**：多线程同时上传同一展品，修订号严格递增不重号、全部 `ready`、
  仅一个现役、每个修订的瓦片文件全部存在（该用例已连续重复 40+ 次无失败）。
- **构建失败**：注入渲染崩溃，现役清单不动、失败修订落库为 `failed`、暂存被清理。
- **故障/重启恢复**：中断的 `building` 行（死属主）与文件丢失的 `ready` 行被标记
  `failed`，现役指针回退到最高完好修订；属主仍有心跳的在建任务不被误伤。
- **早构建不覆盖新图**：用钩子让修订 2 停在渲染中，修订 3 先上线，修订 2 完成后
  返回 409 且保留为可读历史修订，现役仍是 3。
- 另用 `uvicorn --workers 2` 真实 HTTP 重复验证并发上传、边缘瓦片逐像素重拼、
  杀进程重启后的恢复与现役回退。

## 已知限制 / 未完成项

- 本开发沙箱没有 Docker 守护进程，`Dockerfile` 与 `docker-compose.yml` 已编写并做
  YAML/结构校验，但**未在真实 Docker 中构建运行**；容器外已用 uvicorn 单进程与
  `--workers 2`（真实 HTTP，多进程共享同一 SQLite 与瓦片卷）完成功能与并发验证。
- 沙箱 PyPI 镜像最高提供 Pillow 10.1.0，故 `requirements.txt` 固定该版本；所用 API
  （`Image.Resampling.LANCZOS`、`crop/resize/save`）在该版本与更新版本均可用。
- 属主心跳过期阈值默认 30 秒，可用环境变量 `EXHIBITION_OWNER_STALE_SECONDS` 调整；
  机器时钟大幅漂移可能影响过期判定。
- 旧修订目录不会自动垃圾回收（换图后历史版本按要求保留可读）；目前没有删除展品/
  修订的接口，长期积累的磁盘空间需要运维侧另行清理。
- 同一 `page_view_id` 若出现多个 open，取最早 open；多个 close 取最早 close，
  异常上报不报错但只保留一组配对。
- 没有鉴权/限流与分页；观众号、事件号均按字符串处理，未做格式白名单。
- 撤回是硬删除事件数据（仅保留撤回标记本身），不做审计留档。
