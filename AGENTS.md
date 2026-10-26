# AGENTS.md — 给 AI Agent 的智云课堂导出指南

本仓库是一个浙江大学智云课堂（`classroom.zju.edu.cn`）回放导出工具：把某节课的字幕整理成带时间轴的文字稿，同时下载平台生成的课件图片。

如果你是 AI Agent，目标很简单：

> **用户只需要提供智云学号、密码和想导出的课程/讲次。你负责登录、定位课时、导出，并把结果路径和统计告诉用户。**

---

## 1. 仓库里有什么

```text
zhiyun/                     # Python CLI（登录、API、导出）
tests/                      # 单元测试
.claude/skills/zhiyun/      # 可直接安装的 Claude Code Skill
AGENTS.md                   # 本文件
README.md                   # 面向人的说明
.env.example                # 凭证模板（.env 本身被 gitignore）
```

- CLI 入口：`python3 -m zhiyun`；安装后也可以直接 `zhiyun`
- 依赖：`requests`；浏览器登录兜底需要 Playwright
- 正常的账号密码登录不需要浏览器

## 2. 给 Agent 的最短路径

### 2.1 定位仓库并检查环境

```bash
ZHIYUN_REPO="${ZHIYUN_REPO:-$HOME/projects/zhiyun}"
cd "$ZHIYUN_REPO"
python3 -m zhiyun --help
```

如果 `zhiyun` 已经在 PATH 中，可以直接用它。否则用 `python3 -m zhiyun`，并确保当前目录是仓库根目录（包含 `pyproject.toml` 的目录），这样 `.env` 会被自动读取。

### 2.2 只问用户三件事

1. 智云学号
2. 统一身份认证密码
3. 课程名/课程 ID；如果用户不确定，先列课程让用户选，再列课时

如果用户已经说过课程和讲次，不要重复问。只有缺少 `.env` 或凭证失效时才问密码。

### 2.3 首次登录：写入本地 .env 并登录

在仓库根目录创建 `.env`：

```bash
cat > .env <<'ENV'
A = <学号>
P = <密码>
ENV
chmod 600 .env
```

然后：

```bash
python3 -m zhiyun login
```

成功标志：出现“验证通过：当前账号可访问 N 门课程”。

- **不要把密码复述到对话里，也不要把 `.env` 提交到 git。**
- 如果自动登录失败（验证码/二次验证/学校改流程）：
  ```bash
  python3 -m zhiyun login --browser --force
  ```
  让用户在打开的浏览器里完成登录。
- 已有 `.env` 时先检查：
  ```bash
  python3 -m zhiyun status --check
  ```
  通过就跳过登录。JWT 通常约 1 天有效，过期后重新 `login` 即可，不用再问用户密码（`.env` 里已经有了）。

### 2.4 定位课程和课时

```bash
python3 -m zhiyun courses --json
python3 -m zhiyun lectures "<课程名或课程 ID>" --json
```

- 课程字段：`course_id`, `title`, `teacher`, `term`
- 课时字段：`index`, `sub_id`, `title`, `start_at`, `has_transcript`
- 用户说“最新一讲” → 用 `--lecture last`（会自动找最新有字幕的课时）
- 优先选 `has_transcript=true` 的课时；无字幕的可加 `--try-transcript` 再试
- 课程列表为空时，让用户从智云课程详情页 URL 取 `course_id=` 后面的数字，直接：
  ```bash
  python3 -m zhiyun export <course_id> --lecture last
  ```

### 2.5 导出

```bash
# 单节：最新 / 指定序号 / sub_id / 标题
python3 -m zhiyun export "<课程>" --lecture last --quiet
python3 -m zhiyun export "<课程>" --lecture 3 --quiet
python3 -m zhiyun export "<课程>" --sub-id 1983371 --quiet
python3 -m zhiyun export "<课程>" --title "期中复习" --quiet

# 只导文字 / 不下载图片
python3 -m zhiyun export "<课程>" --lecture last --transcript-only
python3 -m zhiyun export "<课程>" --lecture last --no-images

# 整门课（只导有字幕课时；可能耗时间和磁盘）
python3 -m zhiyun export "<课程>" --all --quiet
```

默认输出根目录：`./zhiyun_output/`。可以用 `-o <dir>` 指定。

### 2.6 汇报给用户

固定包含：

1. 课程、课时、日期
2. **输出绝对路径**
3. 字幕段数、课件图片张数
4. 提醒：字幕是平台 ASR 原文（可能有错字、语气词、课前闲聊）；课件图是平台屏幕帧（可能混入桌面/浏览器画面）
5. 如果用户需要总结：读取 `transcript.md` 后总结，**不要把整份字幕粘贴到对话里**

## 3. 输出结构

```text
zhiyun_output/<课程>/<日期_课时>/
├── lecture.md        # 课件图片 + 字幕交错时间轴
├── transcript.md     # 分段文字稿 + 逐句时间轴
├── transcript.txt    # 连续文本
├── transcript.json   # 结构化字幕
├── slides/           # 课件图片
├── metadata.json     # 统计、回放地址、错误/提示
└── raw/              # 仅 --save-raw 时存在
```

## 4. 错误处理

| 现象 | 处理 |
|---|---|
| `凭证问题`、HTTP 403、`Auth Forbidden` | `python3 -m zhiyun login --force`；仍失败用 `--browser` |
| 自动登录被验证码/二次认证拦住 | `python3 -m zhiyun login --browser --force` |
| 课程列表为空 | 让用户提供课程 ID，直接 `export <id>` |
| 课时“无字幕” | 加 `--try-transcript`；仍为空就只导课件 |
| 图片下载失败 | 重新登录后加 `--force` 重试；单张失败不会中断整节课 |
| 想排查接口 | 导出时加 `--save-raw`，保存原始 JSON |
| 输出目录已存在 | 默认复用目录、覆盖文本、跳过已有图片；`--force` 强制重下 |
| Playwright 未安装 | `pip install -e '.[browser]'` 然后 `python -m playwright install chromium` |

## 5. 把这一套做成你自己的 Skill

本仓库已经内置一个可直接使用的 Claude Code Skill：

```text
.claude/skills/zhiyun/SKILL.md
```

### 方式 A：Claude Code 直接使用

在本仓库内运行时，Claude Code 会自动发现 `.claude/skills/zhiyun`。要在任意目录使用：

```bash
mkdir -p ~/.claude/skills
ln -sfn "$PWD/.claude/skills/zhiyun" ~/.claude/skills/zhiyun
export ZHIYUN_REPO="$PWD"    # 建议写进 ~/.zshrc
```

### 方式 B：其他 Agent / 其他仓库

把下面几样复制过去，即可复用：

1. `zhiyun/` 包 + `pyproject.toml` + `README.md`
2. `.claude/skills/zhiyun/SKILL.md`
3. 本 `AGENTS.md`
4. `.env.example`

Skill 的核心只有三步：

1. 定位仓库/CLI（`ZHIYUN_REPO` 或 PATH 上的 `zhiyun`）
2. 向用户要学号+密码或复用 `.env`，跑 `login`
3. `courses` → `lectures` → `export`，按固定格式汇报

如果 Agent 框架不支持 Skill 文件，把本文件作为工具说明注入，或把下面的最小提示词放进系统提示：

> 你可以使用 `<repo>` 下的智云课堂导出 CLI。用户请求导出智云课堂回放时，先确认学号、密码、课程和讲次；把学号密码写入 `<repo>/.env`（不要提交、不要复述密码），运行 `cd <repo> && python3 -m zhiyun login`，再用 `courses`/`lectures`/`export` 完成导出。汇报输出路径、字幕段数、课件张数，并提醒字幕是 ASR 原文、课件图是屏幕帧。

## 6. 约束

- 只导出用户本人有权访问的课程；仅供个人学习使用。
- 不要公开传播课程内容，不要做批量抓取或压力测试。
- 不要提交 `.env`、`zhiyun_output/`、`~/.config/zhiyun/` 或个人课程信息到仓库。
- 平台接口可能变化；遇到 403/空数据时先重新登录，再考虑接口变更。
