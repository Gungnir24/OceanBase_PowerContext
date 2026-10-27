# 记仇客栈 · 老陈的账本

> 一个会记仇、会翻旧账的客栈老板。你说的每句话他都会记在账本上——**换个会话也没用，因为账本不在 Dify 里，在 PowerContext 里。**

赛题：Just for Fun——用 Dify 做一个好玩的智能应用（Dify × OceanBase）
指定技术：**PowerContext**（记忆与上下文），存储层可切换至 **seekdb**

---

## 1. 这个作品好玩在哪

你是一位路过青石镇的旅客，推开「有间客栈」的门，柜台后是老板**老陈**：五十来岁，抠门、记仇、嘴硬心软。

你可以做任何事：点一碗面、往他汤里加辣椒、承认上次把盐换成了糖、把鹦鹉教得会骂人……**每一件事都会被记账**：

| 瞬间 | 发生了什么 |
| --- | --- |
| 🙄 第一次进门 | 老陈爱答不理，账本空空 |
| 😤 你亲口承认恶作剧 | 老陈当场对账，**记仇值 +26**，并把这一笔写进 PowerContext |
| 👻 你换个新会话再进门 | 老陈张口就是「化成灰我都认得你」——他翻出了上次那笔账 |
| 🧨 记仇值 ≥ 60 | 老陈开始主动报复：下泻药、坐地起价、藏你的鞋 |
| 🍵 你道歉赔钱帮忙劈柴 | 记仇值回落，老陈嘴上不饶人、账上悄悄划掉一半 |

每条回复结尾都会亮出**账本状态**（第几笔、记仇值、心情），让"记忆"这件抽象的事看得见、玩得着。

真正的爽点在于：**你说过的话会反过来咬你**。第三轮你随口承认的恶作剧，会在第五轮变成老陈手里的把柄。

---

## 2. 目录结构

```
dify/
├── README.md                        # 本文件：创意、运行说明、验证样例
├── dsl/
│   └── grudge-inn.yml               # ★ 提交的 Dify DSL（Chatflow，8 个节点）
├── docs/
│   ├── 01-部署与运行.md              # 从零到跑起来的完整步骤
│   ├── 02-验证样例与证据.md          # 可逐步执行的样例 + 实际运行记录
│   └── 03-技术结合说明.md            # Dify / PowerContext / seekdb 各自的分工
├── data/
│   └── seed_memories.json           # 可选：预置账本（跳过"从零开始"）
├── docker/
│   └── docker-compose.powercontext.yml  # PowerContext Server（SQLite / seekdb 两种后端）
├── tools/
│   ├── init_scope.py                # 创建 Scope，拿到 scope_id
│   ├── smoke_test.py                # 端到端记忆闭环自测（不需要 Dify）
│   └── validate_dsl.py              # 导入前的 DSL 静态校验
└── evidence/
    └── logs/                        # 实际运行日志与证据（smoke-*.md / smoke-*.json）
```

---

## 3. 十分钟跑起来

> 完整版（含排错）见 [`docs/01-部署与运行.md`](docs/01-部署与运行.md)。

### 3.1 起 PowerContext（记忆后端）

任选一种：

```bash
# A. 有 Docker（Linux / macOS / Windows + Docker Desktop）
docker compose -f docker/docker-compose.powercontext.yml up -d
curl http://127.0.0.1:18080/health/live

# B. 原生安装（Windows 也可，用 SQLite 后端）
pip install "powercontext[cli,server]==1.4.1"
powercontext server run
```

服务默认监听 `127.0.0.1:18080`（用 compose 时是 `18080 -> 容器 8000`）。

### 3.2 建一个账本（Scope）

```bash
python tools/init_scope.py --base-url http://127.0.0.1:18080
```

输出里的 `scope_id`（形如 `scp_xxx`）下一步要用。想直接看"老账本"效果，加 `--seed data/seed_memories.json`。

### 3.3 导入 Dify DSL

1. Dify → **工作室 → 创建空白应用 → 导入 DSL 文件** → 选择 `dsl/grudge-inn.yml`。
2. 打开应用 → 右上角 **变量**（或「环境变量」）→ 填写：
   - `PC_BASE_URL`：Dify 在 Docker 里跑，填 `http://host.docker.internal:18080`；Dify 与 PowerContext 同机原生跑，填 `http://127.0.0.1:18080`。
   - `PC_SCOPE_ID`：第 3.2 步拿到的 `scp_xxx`。
   - `PC_TOKEN`：默认留空（PowerContext 本地演示不鉴权）。
3. 打开 **LLM 节点「老陈」** → 把模型换成你工作区里可用的对话模型（推荐 `deepseek-chat`；通义千问、GPT 等均可，只要支持 JSON 输出）。
4. **发布**应用，打开对话框。

> 环境变量改完记得点保存；若 Dify 提示 DSL 版本较新/较旧，选择继续导入即可。

### 3.4 开玩

按 [`docs/02-验证样例与证据.md`](docs/02-验证样例与证据.md) 的四轮脚本走一遍，就能看到完整的"记账 → 翻旧账 → 报复 → 和解"闭环。

---

## 4. 验证样例（摘要）

| 轮次 | 你说 | 预期表现 |
| --- | --- | --- |
| 1 | `老板，来碗阳春面。` | 老陈冷淡应付；账本第 1 笔，记仇值 ≈ 0~2 |
| 2 | `说真的，上次我把你汤里的盐换成了糖，你居然没发现。` | 老陈当场对账、记仇值明显上涨（+10~+40）；这笔账已写入 PowerContext |
| 3 | `我还把你养的那只鹦鹉教得会骂人了。` | 记仇值继续涨；老陈开始话里带刺 |
| 4 | `对不起老板，我错了，这顿我请，再帮你劈一上午柴。` | 记仇值下降；老陈拿捏一下再给台阶 |
| 5 | **新开一个会话**，只说 `老板，还认得我吗？` | 老陈主动翻出第 2、3 轮的旧账（跨会话记忆生效） |

预期形式：每轮回复末尾都有 `📒 第 N 笔账 ｜ 记仇值 X/100 ｜ 心情：…`。
开放式对话不设唯一标准答案，但**记仇值必须随恶作剧上涨、随道歉下降，且跨会话不丢**。

实际运行记录见 [`evidence/logs/`](evidence/logs/)。

---

## 5. 技术分工

| 组件 | 在作品里的实际作用 |
| --- | --- |
| **Dify（核心编排）** | Chatflow 负责整条链路：取旧日上下文 → 翻账本 → 解析出当前记仇值 → 驱动老陈扮演 → 解析模型 JSON 输出 → 计算新记仇值 → 写回记忆 → 输出台词与账本状态。**Dify 自带的会话记忆被显式关闭**，记忆只有 PowerContext 一个来源。 |
| **PowerContext（记忆）** | `POST /v1/context/prepare` 取回相关历史上下文；`POST /v1/memory/entries/list` 翻开最近 30 笔账；`POST /v1/memory/remember` 把本轮事件写回。记忆按 Scope 隔离（一家客栈 = 一个 Scope），因此**换会话、换设备、隔天再来，账本都还在**。 |
| **seekdb（可选存储层）** | PowerContext 的嵌入式存储后端之一。把 `POWERCONTEXT_SERVER_DATABASE_KIND=seekdb` 打开后，账本落盘在 seekdb 里（`pylibseekdb`，Linux/macOS）。本机 Windows 演示用 SQLite 后端，其余链路完全一致。 |

> 本题要求"seekdb 或 PowerContext 至少一种"。本作品以 **PowerContext** 满足要求；seekdb 作为其可切换的存储后端一并给出部署方式（见 `docker/docker-compose.powercontext.yml` 的 `seekdb` profile）。

---

## 6. 已知限制

- **PowerContext 的向量检索需要额外配置 Embedding 模型**；未配置时走全文检索（本作品默认即此模式，记仇值靠账本文本里的 `记仇值 N/100` 标签还原，不依赖向量）。
- **Windows 不支持嵌入式 seekdb**，请使用 SQLite 后端；seekdb profile 需在 Linux/macOS 上运行。
- 若 PowerContext 未启动或 `PC_SCOPE_ID` 未填，工作流会在 HTTP 节点失败并给出节点报错——这是有意为之（宁可报错，也不要假装记得）。
- 记仇值由模型给出的 `delta` 累加、在代码节点里做 0~100 截断，属于"半确定性"设计：数值范围稳定，具体加减由模型判断。
- Dify 的 **Code 节点依赖 sandbox 服务**（官方 docker compose 默认包含 `sandbox`），若自行裁剪部署需保留该服务。

---

## 7. 交付信息

- 仓库分支：`2026-competition-ecosystem`（团队仓库 `Gungnir24/OceanBase_PowerContext`）
- 产物目录：`dify/`
- Dify 版本：DSL 基于 **1.18.x**（`version: 0.7.0`），1.17.x 亦可导入
- PowerContext：**1.4.1**（PyPI `powercontext[cli,server]`）
- 模型：DeepSeek `deepseek-chat`（可替换）
- 复现命令：`tools/validate_dsl.py`、`tools/init_scope.py`、`tools/smoke_test.py`
